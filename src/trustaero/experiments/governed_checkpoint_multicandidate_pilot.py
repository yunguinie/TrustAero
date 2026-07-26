"""Frozen development pilot for three governed checkpoint candidates.

The pilot is a label-diversity gate, not an optimizer evaluation.  It uses all
six candidate orders equally, groups inference by complete scenario and seed,
and retains a failure when the fused baseline or one checkpoint plan dominates
the entire matrix.  Only a pass permits later optimizer training.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.execution_aware_oracle_stability import (
    classify_ratio_interval,
)
from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _environment,
    _git_state,
)
from trustaero.experiments.execution_flow_inference import (
    hierarchical_paired_log_ratio_ci,
)
from trustaero.experiments.governed_checkpoint_multicandidate import (
    FUSED,
    MULTICANDIDATE_IDS,
    GovernedCheckpointCandidate,
    build_governed_checkpoint_candidate,
    checkpoint_candidate_feasibility,
    execute_governed_checkpoint_candidate,
    result_digest,
)
from trustaero.experiments.governed_checkpoint_reversal import (
    POLICY_FIRST,
    QUERY_FIRST,
    GovernedCheckpointUnit,
    _create_data,
    _digest,
    checkpoint_orders,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy


@dataclass(frozen=True, slots=True)
class MultiCandidatePilotConfig:
    """Frozen grid, balanced timing controls, and stop/go gates."""

    results_dir: str
    row_counts: tuple[int, ...]
    identifier_widths: tuple[int, ...]
    policy_selectivities: tuple[float, ...]
    query_selectivities: tuple[float, ...]
    seeds: tuple[int, ...]
    candidate_ids: tuple[str, ...]
    warmup_rounds_per_permutation: int
    measured_rounds_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    minimum_conclusive_scenario_rate: float
    minimum_distinct_singleton_winners: int
    maximum_dominant_singleton_winner_fraction: float
    require_fused_singleton_winner: bool
    require_checkpoint_singleton_winner: bool
    require_clean_git: bool

    def __post_init__(self) -> None:
        dimensions: tuple[tuple[object, ...], ...] = (
            cast(tuple[object, ...], self.row_counts),
            cast(tuple[object, ...], self.identifier_widths),
            cast(tuple[object, ...], self.policy_selectivities),
            cast(tuple[object, ...], self.query_selectivities),
            cast(tuple[object, ...], self.seeds),
        )
        if any(not values or len(values) != len(set(values)) for values in dimensions):
            raise ValueError("Multi-candidate pilot dimensions must be unique")
        if self.candidate_ids != MULTICANDIDATE_IDS:
            raise ValueError("Multi-candidate pilot must retain all three candidates")
        if len(self.seeds) < 3:
            raise ValueError("Multi-candidate pilot requires at least three seeds")
        if self.warmup_rounds_per_permutation < 1:
            raise ValueError("Multi-candidate pilot requires balanced warmup")
        if self.measured_rounds_per_permutation < 5:
            raise ValueError("Multi-candidate pilot requires five permutation rounds")
        if any(value <= 0 for value in self.row_counts):
            raise ValueError("Multi-candidate row counts must be positive")
        selectivities = self.policy_selectivities + self.query_selectivities
        if any(not 0.0 < value < 1.0 for value in selectivities):
            raise ValueError("Multi-candidate selectivities must be in (0, 1)")
        if any(not 1 <= value <= 4096 for value in self.identifier_widths):
            raise ValueError("Multi-candidate widths must be in [1, 4096]")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("Multi-candidate DuckDB controls are invalid")
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("Multi-candidate tie fraction is invalid")
        if not 0.5 < self.confidence_level < 1.0 or self.bootstrap_draws < 1000:
            raise ValueError("Multi-candidate inference controls are invalid")
        if not 0.0 <= self.minimum_conclusive_scenario_rate <= 1.0:
            raise ValueError("Multi-candidate conclusive-rate gate is invalid")
        if self.minimum_distinct_singleton_winners < 2:
            raise ValueError("Multi-candidate pilot requires winner diversity")
        if not 0.0 < self.maximum_dominant_singleton_winner_fraction <= 1.0:
            raise ValueError("Multi-candidate dominant-winner gate is invalid")

    @property
    def measured_blocks_per_unit(self) -> int:
        return math.factorial(len(self.candidate_ids)) * self.measured_rounds_per_permutation


def load_multicandidate_pilot_config(
    path: Path | str,
) -> MultiCandidatePilotConfig:
    """Load the frozen development matrix."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return MultiCandidatePilotConfig(
        results_dir=str(payload["results_dir"]),
        row_counts=tuple(int(item) for item in payload["row_counts"]),
        identifier_widths=tuple(int(item) for item in payload["identifier_widths"]),
        policy_selectivities=tuple(float(item) for item in payload["policy_selectivities"]),
        query_selectivities=tuple(float(item) for item in payload["query_selectivities"]),
        seeds=tuple(int(item) for item in payload["seeds"]),
        candidate_ids=tuple(str(item) for item in payload["candidate_ids"]),
        warmup_rounds_per_permutation=int(payload["warmup_rounds_per_permutation"]),
        measured_rounds_per_permutation=int(payload["measured_rounds_per_permutation"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_draws=int(payload["bootstrap_draws"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        minimum_conclusive_scenario_rate=float(payload["minimum_conclusive_scenario_rate"]),
        minimum_distinct_singleton_winners=int(payload["minimum_distinct_singleton_winners"]),
        maximum_dominant_singleton_winner_fraction=float(
            payload["maximum_dominant_singleton_winner_fraction"]
        ),
        require_fused_singleton_winner=bool(payload["require_fused_singleton_winner"]),
        require_checkpoint_singleton_winner=bool(payload["require_checkpoint_singleton_winner"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def multicandidate_pilot_units(
    config: MultiCandidatePilotConfig,
) -> tuple[GovernedCheckpointUnit, ...]:
    """Expand the full frozen matrix without cherry-picking a seed or stratum."""

    return tuple(
        GovernedCheckpointUnit(rows, width, policy, query, seed)
        for rows in config.row_counts
        for width in config.identifier_widths
        for policy in config.policy_selectivities
        for query in config.query_selectivities
        for seed in config.seeds
    )


def _profile_candidate(
    connection: Any,
    candidate: GovernedCheckpointCandidate,
) -> dict[str, object]:
    """Observe a combined fingerprint outside the formal timing sample."""

    connection.execute("DROP TABLE IF EXISTS governed_output")
    connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
    fingerprints: list[str] = []
    operators: list[str] = []
    if candidate.checkpoint_sql is not None:
        observed = observe_duckdb_plan(
            connection,
            candidate.checkpoint_sql,
            analyze=False,
        )
        fingerprints.append(observed.fingerprint)
        operators.extend(observed.operator_names)
        # DuckDB may instantiate a CREATE target while explaining DDL.
        connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
        connection.execute(candidate.checkpoint_sql)
    output = observe_duckdb_plan(connection, candidate.output_sql, analyze=False)
    fingerprints.append(output.fingerprint)
    operators.extend(output.operator_names)
    connection.execute("DROP TABLE IF EXISTS governed_output")
    connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
    return {
        "candidate_id": candidate.candidate_id,
        "combined_fingerprint": _digest(fingerprints),
        "operators": operators,
        "materialization_kind": candidate.materialization_kind,
    }


def _execute_timed(
    connection: Any,
    candidate: GovernedCheckpointCandidate,
    unit: GovernedCheckpointUnit,
    *,
    repeat_index: int,
    order_position: int,
    permutation_id: str,
) -> dict[str, object]:
    started = time.perf_counter()
    checksum = execute_governed_checkpoint_candidate(connection, candidate)
    latency_ms = (time.perf_counter() - started) * 1000.0
    return {
        "scenario_id": unit.scenario_id,
        "unit_id": unit.unit_id,
        "seed": unit.seed,
        "candidate_id": candidate.candidate_id,
        "repeat_index": repeat_index,
        "order_position": order_position,
        "permutation_id": permutation_id,
        "latency_ms": latency_ms,
        "result_digest": result_digest(checksum),
    }


def _feasibility_profiles(
    unit: GovernedCheckpointUnit,
    actual: Mapping[str, int],
) -> dict[str, list[str]]:
    """Validate the four predeclared legal candidate sets."""

    policies = (
        GovernanceFeasibilityPolicy("permissive", None, None),
        GovernanceFeasibilityPolicy("no-raw-checkpoint", None, 0),
        GovernanceFeasibilityPolicy(
            "checkpoint-required",
            None,
            None,
            require_governance_checkpoint=True,
        ),
        GovernanceFeasibilityPolicy(
            "narrow-checkpoint-required",
            None,
            0,
            require_governance_checkpoint=True,
        ),
    )
    observed = {
        policy.policy_id: list(
            checkpoint_candidate_feasibility(
                unit,
                actual,
                policy,
            ).feasible_candidate_ids
        )
        for policy in policies
    }
    expected = {
        "permissive": list(MULTICANDIDATE_IDS),
        "no-raw-checkpoint": [FUSED, POLICY_FIRST],
        "checkpoint-required": [POLICY_FIRST, QUERY_FIRST],
        "narrow-checkpoint-required": [POLICY_FIRST],
    }
    if observed != expected:
        raise ValueError("Multi-candidate governance profiles changed")
    return observed


def _run_unit(
    config: MultiCandidatePilotConfig,
    unit: GovernedCheckpointUnit,
    *,
    blocks_done: int,
    total_blocks: int,
    started: float,
    progress_callback: Callable[[int, int, str, float], None] | None,
) -> dict[str, object]:
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        actual = _create_data(connection, unit)
        candidates = {
            candidate_id: build_governed_checkpoint_candidate(
                candidate_id,
                unit,
                actual,
            )
            for candidate_id in config.candidate_ids
        }
        profiles = {
            candidate_id: _profile_candidate(connection, candidate)
            for candidate_id, candidate in candidates.items()
        }
        if len({str(profile["combined_fingerprint"]) for profile in profiles.values()}) != len(
            config.candidate_ids
        ):
            raise ValueError("Multi-candidate physical plans are not distinct")
        feasibility = _feasibility_profiles(unit, actual)

        stable = int.from_bytes(
            hashlib.sha256(unit.unit_id.encode()).digest()[:4],
            "big",
        )
        warmups = checkpoint_orders(
            config.candidate_ids,
            config.warmup_rounds_per_permutation,
            seed=config.order_seed + stable,
        )
        for repeat_index, order in enumerate(warmups):
            block = [
                _execute_timed(
                    connection,
                    candidates[candidate_id],
                    unit,
                    repeat_index=repeat_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )
                for position, candidate_id in enumerate(order)
            ]
            if len({str(row["result_digest"]) for row in block}) != 1:
                raise ValueError("Multi-candidate warmup results differ")

        orders = checkpoint_orders(
            config.candidate_ids,
            config.measured_rounds_per_permutation,
            seed=config.order_seed + stable + 1,
        )
        measurements: list[dict[str, object]] = []
        for block_index, order in enumerate(orders):
            block = [
                _execute_timed(
                    connection,
                    candidates[candidate_id],
                    unit,
                    repeat_index=block_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )
                for position, candidate_id in enumerate(order)
            ]
            if len({str(row["result_digest"]) for row in block}) != 1:
                raise ValueError("Multi-candidate measured results differ")
            measurements.extend(block)
            if progress_callback is not None:
                progress_callback(
                    blocks_done + block_index + 1,
                    total_blocks,
                    f"{unit.unit_id} block={block_index + 1}",
                    time.perf_counter() - started,
                )
        return {
            "unit": asdict(unit),
            "unit_id": unit.unit_id,
            "actual_cardinalities": actual,
            "feasibility_profiles": feasibility,
            "profiles": profiles,
            "measurements": measurements,
        }
    finally:
        connection.close()


def _write_measurements(
    output_dir: Path,
    payloads: Sequence[Mapping[str, Any]],
) -> None:
    rows: list[dict[str, object]] = []
    for payload in payloads:
        unit = cast(dict[str, object], payload["unit"])
        actual = cast(dict[str, object], payload["actual_cardinalities"])
        for row in cast(list[dict[str, object]], payload["measurements"]):
            rows.append({**unit, **actual, **row})
    with (output_dir / "measurements.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _analyze(
    output_dir: Path,
    config: MultiCandidatePilotConfig,
) -> dict[str, object]:
    with (output_dir / "measurements.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = list(csv.DictReader(handle))
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        families[row["scenario_id"]].append(row)

    scenario_results: list[dict[str, object]] = []
    singleton_winners: list[str] = []
    for scenario_id, family_rows in sorted(families.items()):
        by_block: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in family_rows:
            by_block[(int(row["seed"]), int(row["repeat_index"]))][row["candidate_id"]] = float(
                row["latency_ms"]
            )
        if any(set(values) != set(config.candidate_ids) for values in by_block.values()):
            raise ValueError(f"Incomplete multi-candidate block: {scenario_id}")

        dominated: set[str] = set()
        pairwise: list[dict[str, object]] = []
        for left, right in itertools.combinations(config.candidate_ids, 2):
            ratios: dict[int, list[float]] = defaultdict(list)
            for (seed, _repeat), latencies in sorted(by_block.items()):
                ratios[seed].append(math.log(latencies[left] / latencies[right]))
            stable_seed = int.from_bytes(
                hashlib.sha256(
                    f"{config.bootstrap_seed}:{scenario_id}:{left}:{right}".encode()
                ).digest()[:8],
                "big",
            )
            point, lower, upper = hierarchical_paired_log_ratio_ci(
                ratios,
                confidence_level=config.confidence_level,
                repetitions=config.bootstrap_draws,
                seed=stable_seed,
            )
            conclusion = classify_ratio_interval(
                lower,
                upper,
                config.practical_tie_fraction,
            )
            if conclusion == "LEFT_MATERIALLY_FASTER":
                dominated.add(right)
            elif conclusion == "LEFT_MATERIALLY_SLOWER":
                dominated.add(left)
            pairwise.append(
                {
                    "left_candidate_id": left,
                    "right_candidate_id": right,
                    "left_over_right_ratio": point,
                    "confidence_interval": [lower, upper],
                    "conclusion": conclusion,
                }
            )
        confidence_set = sorted(set(config.candidate_ids) - dominated)
        if len(confidence_set) == 1:
            singleton_winners.append(confidence_set[0])
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "confidence_undominated_candidate_ids": confidence_set,
                "pairwise": pairwise,
                "seed_count": len({int(row["seed"]) for row in family_rows}),
                "paired_block_count": len(by_block),
            }
        )

    counts = Counter(singleton_winners)
    conclusive_rate = len(singleton_winners) / len(scenario_results) if scenario_results else 0.0
    dominant_fraction = max(counts.values()) / len(singleton_winners) if singleton_winners else 1.0
    distinct = sorted(counts)
    checkpoint_wins = sum(counts[candidate_id] for candidate_id in (POLICY_FIRST, QUERY_FIRST))
    gates = {
        "minimum_conclusive_scenario_rate": (
            conclusive_rate >= config.minimum_conclusive_scenario_rate
        ),
        "minimum_distinct_singleton_winners": (
            len(distinct) >= config.minimum_distinct_singleton_winners
        ),
        "maximum_dominant_singleton_winner_fraction": (
            dominant_fraction <= config.maximum_dominant_singleton_winner_fraction
        ),
        "require_fused_singleton_winner": (
            counts[FUSED] > 0 or not config.require_fused_singleton_winner
        ),
        "require_checkpoint_singleton_winner": (
            checkpoint_wins > 0 or not config.require_checkpoint_singleton_winner
        ),
    }
    passed = all(gates.values())
    summary: dict[str, object] = {
        "status": (
            "PASS_MULTICANDIDATE_LABEL_DIVERSITY"
            if passed
            else "FAIL_MULTICANDIDATE_LABEL_DIVERSITY_RETAIN"
        ),
        "scenario_count": len(scenario_results),
        "unit_count": len(multicandidate_pilot_units(config)),
        "singleton_winner_counts": dict(sorted(counts.items())),
        "distinct_singleton_winners": distinct,
        "conclusive_scenario_rate": conclusive_rate,
        "dominant_singleton_winner_fraction": dominant_fraction,
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "scenario_results": scenario_results,
        "optimizer_training_authorized": passed,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This frozen development pilot only decides whether the three-candidate "
            "family has enough internal label diversity to justify optimizer "
            "training. It is not a trained-optimizer or holdout result."
        ),
    }
    _atomic_json(output_dir / "summary.json", summary)
    return summary


def run_multicandidate_pilot(
    config: MultiCandidatePilotConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume with one atomic artifact per completed data unit."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Multi-candidate pilot requires a clean Git commit")
    config_payload = asdict(config)
    config_digest = _digest(config_payload)
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    results_root = root / config.results_dir
    output_dir = results_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    if resume_run_id:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        environment = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != config_digest:
            raise ValueError("Multi-candidate resume configuration changed")
        if environment.get("commit_hash") != commit:
            raise ValueError("Multi-candidate resume commit changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_units": [],
            "status": "running",
        }
        _atomic_json(output_dir / "config.json", config_payload)
        _atomic_json(
            output_dir / "environment.json",
            _environment(commit, dirty, cast(Any, config)),
        )
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})

    units = multicandidate_pilot_units(config)
    completed = set(cast(list[str], checkpoint["completed_units"]))
    remaining = tuple(unit for unit in units if unit.unit_id not in completed)
    started = time.perf_counter()
    blocks_done = 0
    total_blocks = len(remaining) * config.measured_blocks_per_unit
    for unit in remaining:
        payload = _run_unit(
            config,
            unit,
            blocks_done=blocks_done,
            total_blocks=total_blocks,
            started=started,
            progress_callback=progress_callback,
        )
        _atomic_json(output_dir / "units" / f"{unit.unit_id}.json", payload)
        completed.add(unit.unit_id)
        blocks_done += config.measured_blocks_per_unit
        checkpoint["completed_units"] = sorted(completed)
        checkpoint["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_json(checkpoint_path, checkpoint)

    payloads = [
        json.loads((output_dir / "units" / f"{unit.unit_id}.json").read_text(encoding="utf-8"))
        for unit in units
    ]
    _write_measurements(output_dir, payloads)
    summary = _analyze(output_dir, config)
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    checkpoint["label_diversity_status"] = summary["status"]
    _atomic_json(checkpoint_path, checkpoint)
    return output_dir
