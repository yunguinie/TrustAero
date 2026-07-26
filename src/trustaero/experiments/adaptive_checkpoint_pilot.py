"""Development experiment for governance-safe adaptive checkpoint selection.

This experiment does not remeasure or relabel the full-size oracle.  It binds
to the retained V4.1 final-holdout artifact, runs only a 10% calibration sample
for each already-exposed unit, and asks whether paired pilot timings can beat
the frozen 35% threshold.  A future holdout is allowed only if the predeclared
development gates pass.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _environment,
    _git_state,
)
from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    _confidence_oracles,
    _sha256,
)
from trustaero.experiments.governed_checkpoint_reversal import (
    EA1_CANDIDATE_IDS,
    POLICY_FIRST,
    QUERY_FIRST,
    _execute_candidate,
    _feasibility,
    _write_measurements,
    checkpoint_orders,
)
from trustaero.experiments.real_checkpoint_optimizer_calibration import _metrics
from trustaero.experiments.real_checkpoint_optimizer_validation import (
    threshold_candidate,
)
from trustaero.experiments.real_governed_checkpoint_transfer import (
    RealCheckpointUnit,
    _create_real_data,
    _real_statistics_and_medians,
    load_real_checkpoint_transfer_config,
    real_checkpoint_units,
)
from trustaero.optimizer.adaptive_checkpoint import (
    AdaptiveCheckpointConfig,
    PilotLatencyBlock,
    choose_adaptive_checkpoint,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import GovernedCheckpointStatistics


@dataclass(frozen=True, slots=True)
class AdaptivePilotExperimentConfig:
    """Hash-bound development source, pilot budget, and stopping gates."""

    results_dir: str
    source_measurement_run: str
    source_measurement_config_path: str
    expected_source_summary_sha256: str
    expected_source_measurements_sha256: str
    expected_source_config_sha256: str
    pilot_row_count: int
    pilot_warmup_rounds: int
    pilot_repetitions_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    fallback_query_selectivity_threshold: float
    minimum_confidence_family_hit_improvement: float
    minimum_mean_regret_reduction_percent: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_conclusive_pilot_rate: float
    amortization_reuse_count: int
    minimum_amortized_speedup_vs_threshold: float
    require_clean_git: bool
    experiment_role: str = "adaptive_checkpoint_development"

    def __post_init__(self) -> None:
        if self.pilot_row_count != 15_000:
            raise ValueError("Adaptive checkpoint pilot is frozen at 15000 rows")
        if self.pilot_warmup_rounds < 1 or self.pilot_repetitions_per_permutation < 4:
            raise ValueError("Adaptive checkpoint timing budget is too small")
        if self.duckdb_threads != 1 or self.duckdb_memory_limit_mb < 512:
            raise ValueError("Adaptive checkpoint DuckDB controls changed")
        if self.bootstrap_draws < 2_000:
            raise ValueError("Adaptive checkpoint bootstrap budget is too small")
        if self.amortization_reuse_count < 1:
            raise ValueError("Adaptive checkpoint reuse count is invalid")
        if self.experiment_role != "adaptive_checkpoint_development":
            raise ValueError("Adaptive checkpoint scientific role changed")
        hashes = (
            self.expected_source_summary_sha256,
            self.expected_source_measurements_sha256,
            self.expected_source_config_sha256,
        )
        if any(len(value) != 64 for value in hashes):
            raise ValueError("Adaptive checkpoint source hashes must be SHA-256")

    @property
    def measured_blocks_per_unit(self) -> int:
        return math.factorial(len(EA1_CANDIDATE_IDS)) * (self.pilot_repetitions_per_permutation)


def load_adaptive_pilot_config(path: str | Path) -> AdaptivePilotExperimentConfig:
    return AdaptivePilotExperimentConfig(**json.loads(Path(path).read_text(encoding="utf-8")))


def _validate_source_artifact(
    config: AdaptivePilotExperimentConfig,
    root: Path,
) -> tuple[Path, Any]:
    source_run = root / config.source_measurement_run
    summary_path = source_run / "summary.json"
    measurements_path = source_run / "measurements.csv"
    source_config_path = root / config.source_measurement_config_path
    observed = {
        "summary": _sha256(summary_path),
        "measurements": _sha256(measurements_path),
        "config": _sha256(source_config_path),
    }
    expected = {
        "summary": config.expected_source_summary_sha256,
        "measurements": config.expected_source_measurements_sha256,
        "config": config.expected_source_config_sha256,
    }
    if observed != expected:
        raise ValueError("Adaptive checkpoint source artifact hash mismatch")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    environment = json.loads((source_run / "environment.json").read_text(encoding="utf-8"))
    if summary.get("status") != ("PASS_EA1_REAL_OPTIMIZER_FINAL_HOLDOUT_MEASUREMENT_INTEGRITY"):
        raise ValueError("Adaptive checkpoint source measurement did not pass")
    if environment.get("git_dirty") is not False:
        raise ValueError("Adaptive checkpoint source measurement used a dirty tree")
    source_config = load_real_checkpoint_transfer_config(source_config_path)
    return source_run, source_config


def _paired_blocks(
    rows: list[dict[str, object]],
) -> tuple[PilotLatencyBlock, ...]:
    by_block: dict[int, dict[str, float]] = {}
    for row in rows:
        block = int(cast(int, row["repeat_index"]))
        by_block.setdefault(block, {})[str(row["candidate_id"])] = float(
            cast(float, row["latency_ms"])
        )
    result: list[PilotLatencyBlock] = []
    for block, latencies in sorted(by_block.items()):
        if set(latencies) != set(EA1_CANDIDATE_IDS):
            raise ValueError("Adaptive checkpoint pilot block is incomplete")
        result.append(
            PilotLatencyBlock(
                block,
                latencies[POLICY_FIRST],
                latencies[QUERY_FIRST],
            )
        )
    return tuple(result)


def _run_pilot_unit(
    config: AdaptivePilotExperimentConfig,
    full_unit: RealCheckpointUnit,
    root: Path,
    *,
    output_dir: Path,
    completed_blocks: int,
    total_blocks: int,
    started: float,
    progress_callback: Callable[[int, int, str, float], None] | None,
) -> dict[str, object]:
    import duckdb

    pilot_unit = replace(full_unit, row_count=config.pilot_row_count)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = output_dir / "duckdb_temp" / pilot_unit.unit_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(
            "SET temp_directory = '" + str(temp_dir.resolve()).replace("'", "''") + "'"
        )
        actual = _create_real_data(connection, root, pilot_unit)
        feasibility = _feasibility(actual)
        order_seed = (
            config.order_seed
            + pilot_unit.seed
            + int.from_bytes(
                hashlib.sha256(pilot_unit.scenario_id.encode()).digest()[:4],
                "big",
            )
        )
        warmup_rows: list[dict[str, object]] = []
        for repeat_index, order in enumerate(
            checkpoint_orders(
                EA1_CANDIDATE_IDS,
                config.pilot_warmup_rounds,
                seed=order_seed,
            )
        ):
            for position, candidate_id in enumerate(order):
                warmup_rows.append(
                    _execute_candidate(
                        connection,
                        cast(Any, pilot_unit),
                        candidate_id,
                        repeat_index=repeat_index,
                        order_position=position,
                        permutation_id=">".join(order),
                    )
                )
        measurements: list[dict[str, object]] = []
        orders = checkpoint_orders(
            EA1_CANDIDATE_IDS,
            config.pilot_repetitions_per_permutation,
            seed=order_seed + 1,
        )
        for block_index, order in enumerate(orders):
            block_rows = [
                _execute_candidate(
                    connection,
                    cast(Any, pilot_unit),
                    candidate_id,
                    repeat_index=block_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )
                for position, candidate_id in enumerate(order)
            ]
            if len({str(row["result_digest"]) for row in block_rows}) != 1:
                raise ValueError("Adaptive checkpoint pilot candidates disagree")
            measurements.extend(block_rows)
            if progress_callback is not None:
                progress_callback(
                    completed_blocks + block_index + 1,
                    total_blocks,
                    f"{pilot_unit.unit_id} block={block_index + 1}",
                    time.perf_counter() - started,
                )
        statistics_input = GovernedCheckpointStatistics(
            input_rows=actual["input_rows"],
            sensitive_width_bytes=float(pilot_unit.identifier_width),
            estimated_policy_rows=actual["policy_rows"],
            estimated_query_rows=actual["query_rows"],
            estimated_result_rows=actual["result_rows"],
            statistic_provenance="catalog_exact_controlled",
        )
        stable_seed = int.from_bytes(
            hashlib.sha256(f"{config.bootstrap_seed}:{pilot_unit.unit_id}".encode()).digest()[:8],
            "big",
        )
        decision = choose_adaptive_checkpoint(
            statistics_input,
            GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None),
            _paired_blocks(measurements),
            AdaptiveCheckpointConfig(
                practical_tie_fraction=config.practical_tie_fraction,
                confidence_level=config.confidence_level,
                bootstrap_draws=config.bootstrap_draws,
                bootstrap_seed=stable_seed,
                minimum_paired_blocks=config.measured_blocks_per_unit,
                fallback_query_selectivity_threshold=(config.fallback_query_selectivity_threshold),
            ),
        )
        warmup_cost = sum(float(cast(float, row["latency_ms"])) for row in warmup_rows)
        return {
            "unit": asdict(pilot_unit),
            "unit_id": pilot_unit.unit_id,
            "actual_cardinalities": actual,
            "feasibility": feasibility,
            "decision": asdict(decision),
            "warmup_cost_ms": warmup_cost,
            "total_pilot_cost_ms": warmup_cost + decision.pilot_cost_ms,
            "measurements": measurements,
        }
    finally:
        connection.close()


def run_adaptive_checkpoint_pilot(
    config: AdaptivePilotExperimentConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume the bounded pilot without touching the full-size oracle."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Adaptive checkpoint development requires a clean Git commit")
    source_run, source_config = _validate_source_artifact(config, root)
    units = real_checkpoint_units(source_config)
    output_root = root / config.results_dir
    output_root.mkdir(parents=True, exist_ok=True)
    if resume_run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output_dir = output_root / run_id
        output_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(output_dir / "config.json", asdict(config))
        _atomic_json(
            output_dir / "environment.json",
            _environment(commit, dirty, cast(Any, config)),
        )
        checkpoint = {"completed_units": [], "updated_at": datetime.now(UTC).isoformat()}
        _atomic_json(output_dir / "checkpoint.json", checkpoint)
        _atomic_json(output_root / "latest_run.json", {"run_id": run_id})
    else:
        output_dir = output_root / resume_run_id
        if json.loads((output_dir / "config.json").read_text(encoding="utf-8")) != asdict(config):
            raise ValueError("Adaptive checkpoint resume config changed")
        checkpoint = json.loads((output_dir / "checkpoint.json").read_text(encoding="utf-8"))
    completed = set(str(value) for value in checkpoint["completed_units"])
    total_blocks = len(units) * config.measured_blocks_per_unit
    completed_blocks = len(completed) * config.measured_blocks_per_unit
    started = time.perf_counter()
    for unit in units:
        if unit.unit_id in completed:
            continue
        payload = _run_pilot_unit(
            config,
            unit,
            root,
            output_dir=output_dir,
            completed_blocks=completed_blocks,
            total_blocks=total_blocks,
            started=started,
            progress_callback=progress_callback,
        )
        _atomic_json(output_dir / "units" / f"{unit.unit_id}.json", payload)
        completed.add(unit.unit_id)
        completed_blocks += config.measured_blocks_per_unit
        checkpoint = {
            "completed_units": sorted(completed),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _atomic_json(output_dir / "checkpoint.json", checkpoint)
    payloads = [
        json.loads((output_dir / "units" / f"{unit.unit_id}.json").read_text()) for unit in units
    ]
    _write_measurements(output_dir, payloads)
    decisions = [cast(dict[str, object], payload["decision"]) for payload in payloads]
    summary = {
        "status": "PASS_ADAPTIVE_CHECKPOINT_PILOT_DEVELOPMENT_INTEGRITY",
        "unit_count": len(units),
        "scenario_count": len({unit.scenario_id for unit in units}),
        "pilot_row_count": config.pilot_row_count,
        "paired_block_count": total_blocks,
        "reason_counts": dict(Counter(str(decision["reason_code"]) for decision in decisions)),
        "source_measurement_run": str(source_run.relative_to(root)),
        "source_optimizer_retrained": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This uses an already-exposed failed V4.1 holdout solely as adaptive-"
            "optimizer development data. It cannot authorize a paper holdout claim."
        ),
    }
    _atomic_json(output_dir / "summary.json", summary)
    return output_dir


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def evaluate_adaptive_checkpoint_pilot(
    config: AdaptivePilotExperimentConfig,
    *,
    pilot_run_dir: Path,
    project_root: Path,
) -> Path:
    """Compare frozen pilot decisions with the retained full-size oracle."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Adaptive checkpoint evaluation requires a clean Git commit")
    source_run, source_config = _validate_source_artifact(config, root)
    pilot_run = pilot_run_dir.resolve()
    pilot_summary = json.loads((pilot_run / "summary.json").read_text(encoding="utf-8"))
    if pilot_summary.get("status") != ("PASS_ADAPTIVE_CHECKPOINT_PILOT_DEVELOPMENT_INTEGRITY"):
        raise ValueError("Adaptive checkpoint pilot integrity did not pass")
    full_summary = json.loads((source_run / "summary.json").read_text(encoding="utf-8"))
    oracles = _confidence_oracles(full_summary)
    statistics_by_key, medians = _real_statistics_and_medians(source_run)
    decisions: list[dict[str, object]] = []
    for unit in real_checkpoint_units(source_config):
        payload = json.loads(
            (pilot_run / "units" / f"{unit.unit_id}.json").read_text(encoding="utf-8")
        )
        adaptive_selected = str(payload["decision"]["selected_candidate_id"])
        planner_statistics = statistics_by_key[(unit.scenario_id, unit.seed)]
        threshold_selected = threshold_candidate(
            planner_statistics,
            config.fallback_query_selectivity_threshold,
        )
        actual = {
            candidate_id: medians[(unit.scenario_id, unit.seed, candidate_id)]
            for candidate_id in EA1_CANDIDATE_IDS
        }
        best = min(actual.values())
        decisions.append(
            {
                "scenario_id": unit.scenario_id,
                "seed": unit.seed,
                "adaptive_selected_candidate_id": adaptive_selected,
                "adaptive_regret_percent": (actual[adaptive_selected] / best - 1.0) * 100.0,
                "threshold_selected_candidate_id": threshold_selected,
                "threshold_regret_percent": (actual[threshold_selected] / best - 1.0) * 100.0,
                "adaptive_full_latency_ms": actual[adaptive_selected],
                "threshold_full_latency_ms": actual[threshold_selected],
                "total_pilot_cost_ms": float(payload["total_pilot_cost_ms"]),
                "pilot_conclusion": payload["decision"]["pilot_conclusion"],
                "confidence_oracle_candidate_ids": list(oracles[unit.scenario_id]),
            }
        )
    adaptive = _metrics(
        decisions,
        selection_field="adaptive_selected_candidate_id",
        regret_field="adaptive_regret_percent",
        oracles=oracles,
    )
    threshold = _metrics(
        decisions,
        selection_field="threshold_selected_candidate_id",
        regret_field="threshold_regret_percent",
        oracles=oracles,
    )
    conclusive_rate = sum(
        row["pilot_conclusion"]
        in {
            "POLICY_FIRST_MATERIALLY_FASTER",
            "QUERY_FIRST_MATERIALLY_FASTER",
        }
        for row in decisions
    ) / len(decisions)
    reuse = config.amortization_reuse_count
    adaptive_amortized = _mean(
        [
            float(cast(float, row["adaptive_full_latency_ms"]))
            + float(cast(float, row["total_pilot_cost_ms"])) / reuse
            for row in decisions
        ]
    )
    threshold_latency = _mean(
        [float(cast(float, row["threshold_full_latency_ms"])) for row in decisions]
    )
    amortized_speedup = threshold_latency / adaptive_amortized
    adaptive_hit = float(cast(float, adaptive["confidence_family_hit_rate"]))
    threshold_hit = float(cast(float, threshold["confidence_family_hit_rate"]))
    adaptive_mean = float(cast(float, adaptive["mean_regret_percent"]))
    threshold_mean = float(cast(float, threshold["mean_regret_percent"]))
    gates = {
        "minimum_confidence_family_hit_improvement": (
            adaptive_hit - threshold_hit >= config.minimum_confidence_family_hit_improvement
        ),
        "minimum_mean_regret_reduction_percent": (
            threshold_mean - adaptive_mean >= config.minimum_mean_regret_reduction_percent
        ),
        "maximum_mean_regret_percent": (adaptive_mean <= config.maximum_mean_regret_percent),
        "maximum_p95_regret_percent": (
            float(cast(float, adaptive["p95_regret_percent"])) <= config.maximum_p95_regret_percent
        ),
        "maximum_regret_percent": (
            float(cast(float, adaptive["max_regret_percent"])) <= config.maximum_regret_percent
        ),
        "minimum_conclusive_pilot_rate": (conclusive_rate >= config.minimum_conclusive_pilot_rate),
        "minimum_amortized_speedup_vs_threshold": (
            amortized_speedup >= config.minimum_amortized_speedup_vs_threshold
        ),
        "seed_consistency": (float(cast(float, adaptive["seed_consistent_family_rate"])) == 1.0),
        "governance_legality": all(
            row["adaptive_selected_candidate_id"] in EA1_CANDIDATE_IDS for row in decisions
        ),
    }
    passed = all(gates.values())
    result = {
        "status": (
            "PASS_ADAPTIVE_CHECKPOINT_PILOT_DEVELOPMENT"
            if passed
            else "FAIL_ADAPTIVE_CHECKPOINT_PILOT_DEVELOPMENT_RETAIN"
        ),
        "adaptive_metrics": adaptive,
        "threshold_metrics": threshold,
        "conclusive_pilot_rate": conclusive_rate,
        "amortization_reuse_count": reuse,
        "adaptive_amortized_mean_latency_ms": adaptive_amortized,
        "threshold_mean_latency_ms": threshold_latency,
        "amortized_speedup_vs_threshold": amortized_speedup,
        "gates": gates,
        "decisions": decisions,
        "reason_counts": pilot_summary["reason_counts"],
        "source_measurement_run": str(source_run.relative_to(root)),
        "pilot_run": str(pilot_run.relative_to(root)),
        "evaluation_commit_hash": commit,
        "evaluation_git_dirty": dirty,
        "holdout_claim_authorized": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "A pass only authorizes freezing an adaptive optimizer for a new "
            "untouched-month holdout. These exposed months remain development data."
        ),
    }
    output_root = root / f"{config.results_dir}_evaluation"
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "evaluation.json", result)
    _atomic_json(output_root / "latest_run.json", {"run_id": run_id})
    return output_dir
