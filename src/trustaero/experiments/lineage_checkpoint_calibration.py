"""Grouped development calibration for Lineage checkpoint selection."""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
    fit_nonnegative_analytic_cost,
    grouped_outer_validation,
    select_lambda_inside_training,
)
from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.optimizer.lineage_checkpoint_space import (
    LATE_PER_QUERY_CAPTURE,
    LINEAGE_CHECKPOINT_CANDIDATE_IDS,
    POLICY_LINEAGE_CHECKPOINT,
    SNAPSHOT_LINEAGE_CHECKPOINT,
    LineageCheckpointStatistics,
    build_lineage_checkpoint_profiles,
)

EQUIVALENCE_GROUP = "record_lineage_checkpoint_reuse_v1"
STABLE_PREFERENCES = {EQUIVALENCE_GROUP: LATE_PER_QUERY_CAPTURE}
LAMBDA_GRID = (0.01, 0.1, 1.0, 10.0)


@dataclass(frozen=True, slots=True)
class BaselineDecision:
    """One deployment decision and its observed regret."""

    scenario_id: str
    seed: int
    selected_candidate_id: str
    oracle_candidate_ids: tuple[str, ...]
    oracle_hit: bool
    regret_percent: float


def _work_features(
    metrics: tuple[tuple[str, float], ...],
) -> tuple[tuple[str, float], ...]:
    """Keep identifiable physical work in stable million-unit scales."""

    values = dict(metrics)
    return tuple(
        sorted(
            {
                "checkpoint.rows_million": values["checkpoint.rows"] / 1_000_000.0,
                "checkpoint.scan_rows_million": (values["checkpoint.scan_rows"] / 1_000_000.0),
                "lineage.hash_rows_million": (values["lineage.hash_rows"] / 1_000_000.0),
                "lineage.output_edges_million": (values["lineage.output_edges"] / 1_000_000.0),
                "pipeline_breaker.count": values["pipeline_breaker.count"],
                "source.scan_rows_million": (values["source.scan_rows"] / 1_000_000.0),
            }.items()
        )
    )


def load_lineage_calibration_observations(
    run_dir: Path,
    *,
    allow_complete_admission_negative: bool = False,
) -> tuple[CalibrationObservation, ...]:
    """Bind paired median labels to pre-execution physical work vectors."""

    run_dir = run_dir.resolve()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    accepted_statuses = {"PASS_LINEAGE_CHECKPOINT_OPTIMIZER_ADMISSION"}
    if allow_complete_admission_negative:
        accepted_statuses.add("FAIL_LINEAGE_CHECKPOINT_OPTIMIZER_ADMISSION_RETAIN")
    if summary.get("status") not in accepted_statuses:
        raise ValueError("Lineage calibration requires a passed admission run")
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    labels: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    for row in rows:
        key = (row["scenario_id"], int(row["seed"]), row["candidate_id"])
        labels[key].append(float(row["latency_ms"]))

    units = {
        (str(item["scenario"]["scenario_id"]), int(item["seed"])): item for item in summary["units"]
    }
    input_rows = _input_rows_from_manifest(run_dir)
    observations: list[CalibrationObservation] = []
    for (scenario_id, seed), unit in sorted(units.items()):
        scenario = unit["scenario"]
        policy_selectivities = tuple(float(value) for value in scenario["policy_selectivities"])
        query_selectivities = tuple(float(value) for value in scenario["query_selectivities"])
        query_count = int(scenario["query_count"])
        # These are optimizer-visible cardinality estimates derived from the
        # frozen workload and catalog row count.  No executed candidate count,
        # output evidence, winner, or latency is allowed into the feature side.
        estimated_policy_rows = round(input_rows * sum(policy_selectivities))
        estimated_result_rows = round(
            input_rows
            * sum(
                policy_selectivities[index % len(policy_selectivities)]
                * query_selectivities[index % len(query_selectivities)]
                for index in range(query_count)
            )
        )
        statistics_row = LineageCheckpointStatistics(
            input_rows=input_rows,
            query_count=query_count,
            distinct_policy_count=len(policy_selectivities),
            total_result_rows=estimated_result_rows,
            total_distinct_policy_rows=estimated_policy_rows,
        )
        profiles = build_lineage_checkpoint_profiles(statistics_row)
        for profile in profiles:
            observations.append(
                CalibrationObservation(
                    scenario_id=scenario_id,
                    seed=seed,
                    equivalence_group=EQUIVALENCE_GROUP,
                    candidate_id=profile.candidate_id,
                    latency_ms=statistics.median(labels[(scenario_id, seed, profile.candidate_id)]),
                    features=_work_features(profile.work_metrics),
                )
            )
    return tuple(observations)


def _input_rows_from_manifest(run_dir: Path) -> int:
    """Read the bound row count from the repository-relative config."""

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / str(summary["config_path"])).read_text(encoding="utf-8"))
    return int(config["row_count"])


def _groups(
    observations: tuple[CalibrationObservation, ...],
) -> dict[tuple[str, int], tuple[CalibrationObservation, ...]]:
    grouped: dict[tuple[str, int], list[CalibrationObservation]] = defaultdict(list)
    for item in observations:
        grouped[(item.scenario_id, item.seed)].append(item)
    return {key: tuple(value) for key, value in grouped.items()}


def _decision(
    candidates: tuple[CalibrationObservation, ...],
    selected: str,
    practical_tie_fraction: float,
) -> BaselineDecision:
    actual = {item.candidate_id: item.latency_ms for item in candidates}
    best = min(actual.values())
    oracle = tuple(
        sorted(
            candidate_id
            for candidate_id, value in actual.items()
            if value <= best * (1.0 + practical_tie_fraction)
        )
    )
    return BaselineDecision(
        scenario_id=candidates[0].scenario_id,
        seed=candidates[0].seed,
        selected_candidate_id=selected,
        oracle_candidate_ids=oracle,
        oracle_hit=selected in oracle,
        regret_percent=(actual[selected] / best - 1.0) * 100.0,
    )


def _metrics(decisions: list[BaselineDecision]) -> dict[str, Any]:
    regrets = sorted(item.regret_percent for item in decisions)
    p95 = regrets[min(len(regrets) - 1, math.ceil(0.95 * len(regrets)) - 1)]
    return {
        "decision_count": len(decisions),
        "oracle_set_hit_rate": statistics.mean(item.oracle_hit for item in decisions),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": p95,
        "maximum_regret_percent": max(regrets),
        "decisions": [asdict(item) for item in decisions],
    }


def evaluate_fixed_baselines(
    observations: tuple[CalibrationObservation, ...],
    *,
    practical_tie_fraction: float,
) -> dict[str, dict[str, Any]]:
    """Evaluate every fixed legal strategy on all grouped labels."""

    grouped = _groups(observations)
    return {
        candidate_id: _metrics(
            [
                _decision(candidates, candidate_id, practical_tie_fraction)
                for candidates in grouped.values()
            ]
        )
        for candidate_id in LINEAGE_CHECKPOINT_CANDIDATE_IDS
    }


def _scenario_query_counts(
    run_dir: Path,
) -> dict[str, int]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return {
        str(unit["scenario"]["scenario_id"]): int(unit["scenario"]["query_count"])
        for unit in summary["units"]
    }


def evaluate_grouped_threshold_baseline(
    observations: tuple[CalibrationObservation, ...],
    *,
    scenario_query_counts: dict[str, int],
    practical_tie_fraction: float,
) -> dict[str, Any]:
    """Learn two query-count thresholds inside each outer training fold."""

    grouped = _groups(observations)
    scenarios = sorted({item.scenario_id for item in observations})
    decisions: list[BaselineDecision] = []
    folds: list[dict[str, object]] = []
    counts = sorted(set(scenario_query_counts.values()))
    thresholds = [(lower, upper) for lower in counts for upper in counts if lower <= upper]

    def choose(query_count: int, lower: int, upper: int) -> str:
        if query_count <= lower:
            return LATE_PER_QUERY_CAPTURE
        if query_count <= upper:
            return POLICY_LINEAGE_CHECKPOINT
        return SNAPSHOT_LINEAGE_CHECKPOINT

    for held_out in scenarios:
        training_groups = {key: value for key, value in grouped.items() if key[0] != held_out}
        scored: list[tuple[float, int, int]] = []
        for lower, upper in thresholds:
            regrets = [
                _decision(
                    candidates,
                    choose(scenario_query_counts[scenario_id], lower, upper),
                    practical_tie_fraction,
                ).regret_percent
                for (scenario_id, _seed), candidates in training_groups.items()
            ]
            scored.append((statistics.mean(regrets), lower, upper))
        _score, lower, upper = min(scored)
        for (scenario_id, _seed), candidates in grouped.items():
            if scenario_id == held_out:
                decisions.append(
                    _decision(
                        candidates,
                        choose(scenario_query_counts[scenario_id], lower, upper),
                        practical_tie_fraction,
                    )
                )
        folds.append(
            {
                "held_out_scenario_id": held_out,
                "lower_query_count_threshold": lower,
                "upper_query_count_threshold": upper,
            }
        )
    return {**_metrics(decisions), "folds": folds}


def fit_query_count_threshold(
    observations: tuple[CalibrationObservation, ...],
    *,
    scenario_query_counts: dict[str, int],
    practical_tie_fraction: float,
) -> dict[str, Any]:
    """Fit the deployable simple baseline on all development scenarios."""

    grouped = _groups(observations)
    counts = sorted(set(scenario_query_counts.values()))
    scored: list[tuple[float, int, int, list[BaselineDecision]]] = []
    for lower in counts:
        for upper in counts:
            if lower > upper:
                continue
            decisions: list[BaselineDecision] = []
            for (scenario_id, _seed), candidates in grouped.items():
                query_count = scenario_query_counts[scenario_id]
                if query_count <= lower:
                    selected = LATE_PER_QUERY_CAPTURE
                elif query_count <= upper:
                    selected = POLICY_LINEAGE_CHECKPOINT
                else:
                    selected = SNAPSHOT_LINEAGE_CHECKPOINT
                decisions.append(_decision(candidates, selected, practical_tie_fraction))
            scored.append(
                (
                    statistics.mean(item.regret_percent for item in decisions),
                    lower,
                    upper,
                    decisions,
                )
            )
    _score, lower, upper, decisions = min(scored, key=lambda item: (item[0], item[1], item[2]))
    return {
        "lower_query_count_threshold": lower,
        "upper_query_count_threshold": upper,
        **_metrics(decisions),
    }


def analyze_lineage_checkpoint_calibration(
    run_dir: Path,
    output_dir: Path,
    *,
    practical_tie_fraction: float = 0.03,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run grouped development validation and frozen gate checks."""

    observations = load_lineage_calibration_observations(run_dir)
    validation = grouped_outer_validation(
        observations,
        lambda_grid=LAMBDA_GRID,
        stable_preferences=STABLE_PREFERENCES,
        practical_tie_fraction=practical_tie_fraction,
        progress_callback=progress,
    )
    fixed = evaluate_fixed_baselines(
        observations,
        practical_tie_fraction=practical_tie_fraction,
    )
    best_fixed_id, best_fixed = min(
        fixed.items(),
        key=lambda item: (
            item[1]["mean_regret_percent"],
            item[0],
        ),
    )
    threshold = evaluate_grouped_threshold_baseline(
        observations,
        scenario_query_counts=_scenario_query_counts(run_dir),
        practical_tie_fraction=practical_tie_fraction,
    )
    final_threshold = fit_query_count_threshold(
        observations,
        scenario_query_counts=_scenario_query_counts(run_dir),
        practical_tie_fraction=practical_tie_fraction,
    )
    gates = {
        "minimum_oracle_set_hit_rate": (validation["oracle_set_hit_rate"] >= 0.833),
        "beats_best_fixed_mean_regret": (
            validation["mean_regret_percent"] < best_fixed["mean_regret_percent"]
        ),
        "beats_best_fixed_p95_regret": (
            validation["p95_regret_percent"] < best_fixed["p95_regret_percent"]
        ),
        "maximum_mean_regret": validation["mean_regret_percent"] <= 3.0,
        "maximum_p95_regret": validation["p95_regret_percent"] <= 10.0,
        "not_worse_than_threshold_hit_rate": (
            validation["oracle_set_hit_rate"] >= threshold["oracle_set_hit_rate"]
        ),
        "not_worse_than_threshold_mean_regret": (
            validation["mean_regret_percent"] <= threshold["mean_regret_percent"] + 1e-12
        ),
    }
    passed = all(gates.values())
    selected_lambda = select_lambda_inside_training(
        observations,
        lambda_grid=LAMBDA_GRID,
    )
    final_fit = fit_nonnegative_analytic_cost(
        observations,
        ridge_lambda=selected_lambda,
    )
    payload = {
        "status": (
            "PASS_LINEAGE_CHECKPOINT_COST_MODEL_DEVELOPMENT"
            if passed
            else "FAIL_LINEAGE_CHECKPOINT_COST_MODEL_DEVELOPMENT_RETAIN"
        ),
        "source_run": run_dir.resolve().as_posix(),
        "grouped_validation": validation,
        "fixed_baselines": fixed,
        "best_fixed_candidate_id": best_fixed_id,
        "threshold_baseline": threshold,
        "final_threshold_baseline": final_threshold,
        "gates": gates,
        "final_model": {
            "intercept_ms": final_fit.intercept_ms,
            "coefficients": dict(final_fit.coefficients),
            "ridge_lambda": final_fit.ridge_lambda,
            "converged": final_fit.converged,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "calibration.json", payload)
    return payload
