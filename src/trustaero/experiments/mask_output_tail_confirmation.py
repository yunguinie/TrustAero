"""Targeted paired confirmation of the unstable ``mask_output`` tail case.

This experiment is deliberately narrow.  It does not train an optimizer and it
does not replace the frozen grouped validation.  Instead, it reruns the one
scenario responsible for the observed maximum regret with three isolated,
result-equivalent physical candidates.  Complete permutation blocks make order
effects visible, while seed-level checkpoints keep a long run resumable.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import (
    ExecutionFlowUnit,
    _atomic_json,
    _create_data,
    _environment,
    _execute_variant,
    _git_state,
    _profile_variant,
    _variant_orders,
    execution_flow_variants,
)
from trustaero.experiments.execution_flow_inference import (
    hierarchical_paired_log_ratio_ci,
)

MASK_OUTPUT_CANDIDATE_IDS = (
    "prejoin_mask_materialized_output",
    "postjoin_mask_fused_output",
    "postjoin_raw_materialized_mask_output",
)


@dataclass(frozen=True, slots=True)
class MaskOutputTailConfig:
    """Pre-registered data, repetition, and inference controls."""

    results_dir: str
    row_count: int
    identifier_width: int
    match_rate: float
    seeds: tuple[int, ...]
    candidate_ids: tuple[str, ...]
    warmup_rounds: int
    repetitions_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    require_clean_git: bool

    def __post_init__(self) -> None:
        if not self.results_dir:
            raise ValueError("Tail-confirmation results_dir cannot be empty")
        if self.row_count <= 0 or not 1 <= self.identifier_width <= 4096:
            raise ValueError("Tail-confirmation data dimensions are invalid")
        if not 0.0 <= self.match_rate <= 1.0:
            raise ValueError("Tail-confirmation match_rate must be in [0, 1]")
        if len(self.seeds) < 3 or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("Tail confirmation requires at least three unique seeds")
        if tuple(self.candidate_ids) != MASK_OUTPUT_CANDIDATE_IDS:
            raise ValueError("Tail confirmation must retain the frozen candidate set")
        if self.warmup_rounds < 1 or self.repetitions_per_permutation < 5:
            raise ValueError("Tail confirmation requires warmup and five permutation blocks")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("Tail-confirmation DuckDB limits are invalid")
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("Tail-confirmation practical tie is invalid")
        if not 0.0 < self.confidence_level < 1.0 or self.bootstrap_draws < 1000:
            raise ValueError("Tail-confirmation inference controls are invalid")

    @property
    def permutation_count(self) -> int:
        return math.factorial(len(self.candidate_ids))

    @property
    def measured_blocks_per_seed(self) -> int:
        return self.permutation_count * self.repetitions_per_permutation

    @property
    def measured_execution_count(self) -> int:
        return len(self.seeds) * self.measured_blocks_per_seed * len(self.candidate_ids)


def load_mask_output_tail_config(path: str | Path) -> MaskOutputTailConfig:
    """Load the frozen JSON protocol without accepting implicit defaults."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return MaskOutputTailConfig(
        results_dir=str(payload["results_dir"]),
        row_count=int(payload["row_count"]),
        identifier_width=int(payload["identifier_width"]),
        match_rate=float(payload["match_rate"]),
        seeds=tuple(int(value) for value in payload["seeds"]),
        candidate_ids=tuple(str(value) for value in payload["candidate_ids"]),
        warmup_rounds=int(payload["warmup_rounds"]),
        repetitions_per_permutation=int(payload["repetitions_per_permutation"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_draws=int(payload["bootstrap_draws"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def measurement_orders(
    candidate_ids: Sequence[str],
    repetitions_per_permutation: int,
    *,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    """Return every complete permutation equally often in shuffled block order."""

    permutations = tuple(itertools.permutations(candidate_ids))
    orders = list(permutations) * repetitions_per_permutation
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _config_digest(config: MaskOutputTailConfig) -> str:
    encoded = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _run_seed(
    config: MaskOutputTailConfig,
    seed: int,
    variants: Mapping[str, Any],
    output_dir: Path,
    *,
    completed_blocks: int,
    total_blocks: int,
    started: float,
    progress_callback: Callable[[int, int, str, float], None] | None,
) -> dict[str, Any]:
    """Run one data seed in a fresh DuckDB connection to isolate seed state."""

    import duckdb

    unit = ExecutionFlowUnit(
        row_count=config.row_count,
        identifier_width=config.identifier_width,
        match_rate=config.match_rate,
        seed=seed,
    )
    selected = tuple(variants[candidate_id] for candidate_id in config.candidate_ids)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = output_dir / "duckdb_temp" / f"seed-{seed}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        escaped_temp = str(temp_dir.resolve()).replace("'", "''")
        connection.execute(f"SET temp_directory = '{escaped_temp}'")
        matched_rows, marker_sum = _create_data(connection, unit)

        # One profile per candidate verifies that the three SQL forms still
        # compile to distinct physical plans without mixing profiling timings
        # into the formal latency sample.
        profiles = {
            variant.variant_id: _profile_variant(
                connection,
                unit,
                variant,
                profile_runs=1,
                plan_dir=output_dir / "plans" / unit.unit_id,
            )
            for variant in selected
        }
        if len({str(item["fingerprint"]) for item in profiles.values()}) != len(selected):
            raise ValueError("Tail-confirmation physical candidates are not distinct")

        order_seed = config.order_seed + seed
        warmup_orders = _variant_orders(config.candidate_ids, config.warmup_rounds, seed=order_seed)
        for round_index, order in enumerate(warmup_orders):
            for position, candidate_id in enumerate(order):
                _execute_variant(
                    connection,
                    unit,
                    variants[candidate_id],
                    repeat_index=round_index,
                    order_position=position,
                    is_warmup=True,
                )

        orders = measurement_orders(
            config.candidate_ids,
            config.repetitions_per_permutation,
            seed=order_seed + 1,
        )
        measurements: list[dict[str, object]] = []
        previous_candidate: str | None = None
        for block_index, order in enumerate(orders):
            block_rows: list[dict[str, object]] = []
            for position, candidate_id in enumerate(order):
                row = _execute_variant(
                    connection,
                    unit,
                    variants[candidate_id],
                    repeat_index=block_index,
                    order_position=position,
                    is_warmup=False,
                )
                row["permutation_id"] = ">".join(order)
                row["immediate_predecessor_id"] = previous_candidate
                block_rows.append(row)
                previous_candidate = candidate_id
            if len({str(row["result_digest"]) for row in block_rows}) != 1:
                raise ValueError("Tail-confirmation candidates returned different results")
            measurements.extend(block_rows)
            done = completed_blocks + block_index + 1
            if progress_callback is not None:
                progress_callback(
                    done,
                    total_blocks,
                    f"seed={seed} block={block_index + 1}",
                    time.perf_counter() - started,
                )
        return {
            "unit": asdict(unit),
            "unit_id": unit.unit_id,
            "matched_rows": matched_rows,
            "marker_sum": marker_sum,
            "profiles": profiles,
            "measurements": measurements,
        }
    finally:
        connection.close()


def _write_measurements(output_dir: Path, payloads: Sequence[Mapping[str, Any]]) -> Path:
    rows: list[dict[str, object]] = []
    for payload in payloads:
        unit = cast(dict[str, object], payload["unit"])
        rows.extend(
            {**unit, **row} for row in cast(list[dict[str, object]], payload["measurements"])
        )
    if not rows:
        raise ValueError("Tail-confirmation measurement output is empty")
    path = output_dir / "measurements.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _ratio_drift(log_ratios_by_seed: Mapping[int, Sequence[float]]) -> float:
    first: list[float] = []
    second: list[float] = []
    for values in log_ratios_by_seed.values():
        midpoint = len(values) // 2
        first.extend(values[:midpoint])
        second.extend(values[-midpoint:])
    return abs(math.exp(statistics.median(second) - statistics.median(first)) - 1.0)


def analyze_mask_output_tail(output_dir: Path, config: MaskOutputTailConfig) -> dict[str, Any]:
    """Authorize only conclusions supported by the frozen paired interval."""

    with (output_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    expected_permutations = {
        ">".join(order) for order in itertools.permutations(config.candidate_ids)
    }
    permutation_counts: dict[int, Counter[str]] = defaultdict(Counter)
    by_block: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
    digest_by_block: dict[tuple[int, int], set[str]] = defaultdict(set)
    positions: dict[str, Counter[int]] = defaultdict(Counter)
    for row in rows:
        seed = int(row["seed"])
        block = int(row["repeat_index"])
        candidate = row["variant_id"]
        if candidate == config.candidate_ids[0]:
            permutation_counts[seed][row["permutation_id"]] += 1
        by_block[(seed, block)][candidate] = float(row["latency_ms"])
        digest_by_block[(seed, block)].add(row["result_digest"])
        positions[candidate][int(row["order_position"])] += 1

    integrity = {
        "measurement_count_matches": len(rows) == config.measured_execution_count,
        "every_block_complete": all(
            len(values) == len(config.candidate_ids) for values in by_block.values()
        ),
        "results_equivalent_within_blocks": all(
            len(values) == 1 for values in digest_by_block.values()
        ),
        "all_permutations_equally_repeated": all(
            set(counts) == expected_permutations
            and set(counts.values()) == {config.repetitions_per_permutation}
            for counts in permutation_counts.values()
        ),
        "positions_balanced": all(
            set(counts) == set(range(len(config.candidate_ids))) and len(set(counts.values())) == 1
            for counts in positions.values()
        ),
    }
    if not all(integrity.values()):
        raise ValueError(f"Tail-confirmation integrity failed: {integrity}")

    pairs: list[dict[str, Any]] = []
    for left, right in itertools.combinations(config.candidate_ids, 2):
        ratios: dict[int, list[float]] = defaultdict(list)
        for (seed, _block), latencies in sorted(by_block.items()):
            ratios[seed].append(math.log(latencies[left] / latencies[right]))
        label = f"{left}:{right}"
        stable_seed = int.from_bytes(
            hashlib.sha256(f"{config.bootstrap_seed}:{label}".encode()).digest()[:8],
            "big",
        )
        ratio, lower, upper = hierarchical_paired_log_ratio_ci(
            ratios,
            confidence_level=config.confidence_level,
            repetitions=config.bootstrap_draws,
            seed=stable_seed,
        )
        tie = config.practical_tie_fraction
        if upper < 1.0 / (1.0 + tie):
            conclusion = "LEFT_MATERIALLY_FASTER"
        elif lower > 1.0 + tie:
            conclusion = "LEFT_MATERIALLY_SLOWER"
        else:
            conclusion = "NO_PRACTICAL_DOMINANCE_AUTHORIZED"
        flat = [math.exp(value) for values in ratios.values() for value in values]
        pairs.append(
            {
                "left_candidate_id": left,
                "right_candidate_id": right,
                "median_left_over_right_ratio": ratio,
                "confidence_interval": [lower, upper],
                "conclusion": conclusion,
                "first_second_half_ratio_drift": _ratio_drift(ratios),
                "ratio_p05": _percentile(flat, 0.05),
                "ratio_p95": _percentile(flat, 0.95),
                "paired_block_count": len(flat),
            }
        )

    raw_id = "postjoin_raw_materialized_mask_output"
    fused_id = "postjoin_mask_fused_output"
    raw_vs_fused = next(
        item
        for item in pairs
        if {item["left_candidate_id"], item["right_candidate_id"]} == {raw_id, fused_id}
    )
    raw_is_left = raw_vs_fused["left_candidate_id"] == raw_id
    relation = str(raw_vs_fused["conclusion"])
    raw_advantage = relation == (
        "LEFT_MATERIALLY_FASTER" if raw_is_left else "LEFT_MATERIALLY_SLOWER"
    )
    fused_advantage = relation == (
        "LEFT_MATERIALLY_SLOWER" if raw_is_left else "LEFT_MATERIALLY_FASTER"
    )
    if raw_advantage:
        tail_conclusion = "CONFIRMED_RAW_MATERIALIZATION_ADVANTAGE"
    elif fused_advantage:
        tail_conclusion = "CONFIRMED_FUSED_ADVANTAGE"
    else:
        tail_conclusion = "TAIL_DIFFERENCE_NOT_CONFIDENCE_AUTHORIZED"

    result = {
        "schema_version": 1,
        "status": "PASS_MASK_OUTPUT_TAIL_CONFIRMATION_INTEGRITY",
        "scientific_conclusion": tail_conclusion,
        "integrity_checks": integrity,
        "pairwise_results": pairs,
        "inference": {
            "estimand": "median paired within-block latency ratio",
            "confidence_level": config.confidence_level,
            "bootstrap_draws": config.bootstrap_draws,
            "practical_tie_fraction": config.practical_tie_fraction,
            "data_seed_is_resampling_cluster": True,
        },
        "optimizer_trained": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This diagnostic confirms or rejects one previously observed tail contrast. "
            "It cannot turn the frozen failed optimizer gate into a passing result and "
            "must not be used to tune on a future final holdout."
        ),
    }
    _atomic_json(output_dir / "tail_confirmation.json", result)
    return result


def run_mask_output_tail_confirmation(
    config: MaskOutputTailConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume the isolated experiment with atomic seed checkpoints."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Tail confirmation requires a clean Git commit")
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    results_root = root / config.results_dir
    output_dir = results_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    digest = _config_digest(config)
    if resume_run_id:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        environment = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != digest or environment.get("commit_hash") != commit:
            raise ValueError("Tail-confirmation resume source or config changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": digest,
            "completed_seeds": [],
            "status": "running",
        }
        _atomic_json(output_dir / "config.json", asdict(config))
        _atomic_json(
            output_dir / "environment.json",
            _environment(commit, dirty, cast(Any, config)),
        )
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})

    completed = set(int(value) for value in checkpoint["completed_seeds"])
    variants = {item.variant_id: item for item in execution_flow_variants()}
    started = time.perf_counter()
    total_blocks = len(config.seeds) * config.measured_blocks_per_seed
    session_completed_blocks = len(completed) * config.measured_blocks_per_seed
    for seed in config.seeds:
        if seed in completed:
            continue
        payload = _run_seed(
            config,
            seed,
            variants,
            output_dir,
            completed_blocks=session_completed_blocks,
            total_blocks=total_blocks,
            started=started,
            progress_callback=progress_callback,
        )
        _atomic_json(output_dir / "seeds" / f"seed-{seed}.json", payload)
        completed.add(seed)
        session_completed_blocks += config.measured_blocks_per_seed
        checkpoint["completed_seeds"] = sorted(completed)
        checkpoint["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_json(checkpoint_path, checkpoint)
    payloads = [
        json.loads((output_dir / "seeds" / f"seed-{seed}.json").read_text(encoding="utf-8"))
        for seed in config.seeds
    ]
    _write_measurements(output_dir, payloads)
    result = analyze_mask_output_tail(output_dir, config)
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    checkpoint["scientific_conclusion"] = result["scientific_conclusion"]
    _atomic_json(checkpoint_path, checkpoint)
    return output_dir
