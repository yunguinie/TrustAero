"""Leakage-safe development calibration for governed pipeline costs.

The model predicts latency from candidate-specific physical work, not from a
winner label.  Governance legality is intentionally outside this module: the
hierarchical planner removes illegal candidates before this cost model can
rank the remaining result-equivalent plans.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
    fit_nonnegative_analytic_cost,
    grouped_outer_validation,
    select_lambda_inside_training,
)
from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _git_state,
)
from trustaero.optimizer.governed_pipeline_space import (
    GovernedPipelineStatistics,
    build_governed_pipeline_candidates,
)

EQUIVALENCE_GROUP = "governed-pipeline-checkpoint-v3"


@dataclass(frozen=True, slots=True)
class GovernedPipelineCostCalibrationConfig:
    """Frozen source binding, model choices, and development stop/go gates."""

    results_dir: str
    source_run_dir: str
    source_summary_sha256: str
    lambda_grid: tuple[float, ...]
    stable_preference_candidate_id: str
    practical_tie_fraction: float
    minimum_oracle_set_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_selected_candidate_count: int
    require_not_worse_than_best_fixed_mean: bool
    require_not_worse_than_best_fixed_p95: bool
    require_clean_git: bool

    def __post_init__(self) -> None:
        if not self.results_dir.strip() or not self.source_run_dir.strip():
            raise ValueError("Calibration paths cannot be empty")
        if len(self.source_summary_sha256) != 64:
            raise ValueError("Calibration source SHA-256 is invalid")
        if not self.lambda_grid or any(value < 0.0 for value in self.lambda_grid):
            raise ValueError("Calibration lambda grid is invalid")
        if tuple(sorted(set(self.lambda_grid))) != self.lambda_grid:
            raise ValueError("Calibration lambda grid must be sorted and unique")
        if not 0.0 < self.practical_tie_fraction < 1.0:
            raise ValueError("Calibration tie fraction must be in (0, 1)")
        if not 0.0 <= self.minimum_oracle_set_hit_rate <= 1.0:
            raise ValueError("Calibration hit-rate gate is invalid")
        if (
            min(
                self.maximum_mean_regret_percent,
                self.maximum_p95_regret_percent,
                self.maximum_regret_percent,
            )
            < 0.0
        ):
            raise ValueError("Calibration regret gates cannot be negative")
        if self.minimum_selected_candidate_count < 2:
            raise ValueError("Calibration must select at least two candidates")


def load_governed_pipeline_cost_calibration_config(
    path: Path | str,
) -> GovernedPipelineCostCalibrationConfig:
    """Load the frozen JSON configuration with explicit numeric coercion."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return GovernedPipelineCostCalibrationConfig(
        results_dir=str(payload["results_dir"]),
        source_run_dir=str(payload["source_run_dir"]),
        source_summary_sha256=str(payload["source_summary_sha256"]),
        lambda_grid=tuple(float(value) for value in payload["lambda_grid"]),
        stable_preference_candidate_id=str(payload["stable_preference_candidate_id"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        minimum_oracle_set_hit_rate=float(payload["minimum_oracle_set_hit_rate"]),
        maximum_mean_regret_percent=float(payload["maximum_mean_regret_percent"]),
        maximum_p95_regret_percent=float(payload["maximum_p95_regret_percent"]),
        maximum_regret_percent=float(payload["maximum_regret_percent"]),
        minimum_selected_candidate_count=int(payload["minimum_selected_candidate_count"]),
        require_not_worse_than_best_fixed_mean=bool(
            payload["require_not_worse_than_best_fixed_mean"]
        ),
        require_not_worse_than_best_fixed_p95=bool(
            payload["require_not_worse_than_best_fixed_p95"]
        ),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_governed_pipeline_cost_observations(
    run_dir: Path,
    *,
    expected_summary_sha256: str,
) -> tuple[CalibrationObservation, ...]:
    """Create one median-latency label per complete candidate/seed unit."""

    run_dir = run_dir.resolve()
    summary_path = run_dir / "summary.json"
    if _sha256(summary_path) != expected_summary_sha256:
        raise ValueError("Calibration source summary digest changed")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_GOVERNED_PIPELINE_OPTIMIZER_ADMISSION":
        raise ValueError("Calibration requires a passed admission run")

    observations: list[CalibrationObservation] = []
    unit_paths = sorted((run_dir / "units").glob("*.json"))
    if not unit_paths:
        raise ValueError("Calibration source contains no unit records")
    for unit_path in unit_paths:
        payload = json.loads(unit_path.read_text(encoding="utf-8"))
        unit = payload["unit"]
        actual = payload["actual_cardinalities"]
        measurements = payload["measurements"]
        measured_ids = {str(item["candidate_id"]) for item in measurements}
        scenario_id = str(measurements[0]["scenario_id"])
        statistics = GovernedPipelineStatistics(
            input_rows=int(unit["row_count"]),
            estimated_policy_rows=int(actual["policy_rows"]),
            estimated_query_rows=int(actual["query_rows"]),
            estimated_governed_rows=int(actual["governed_rows"]),
            estimated_query_join_rows=int(actual["query_join_rows"]),
            estimated_result_rows=int(actual["result_rows"]),
            sensitive_width_bytes=float(unit["identifier_width"]),
        )
        profiles = {
            candidate.candidate_id: candidate.profile
            for candidate in build_governed_pipeline_candidates(statistics)
            if candidate.candidate_id in measured_ids
        }
        if set(profiles) != measured_ids:
            raise ValueError(f"Unknown measured candidate: {unit_path.name}")
        for candidate_id, profile in sorted(profiles.items()):
            latencies = [
                float(item["latency_ms"])
                for item in measurements
                if item["candidate_id"] == candidate_id
            ]
            if len(latencies) != 30:
                raise ValueError(f"Calibration requires 30 timings per candidate: {unit_path.name}")
            observations.append(
                CalibrationObservation(
                    scenario_id=scenario_id,
                    seed=int(unit["seed"]),
                    equivalence_group=EQUIVALENCE_GROUP,
                    candidate_id=candidate_id,
                    latency_ms=statistics_module_median(latencies),
                    features=profile.work_metrics,
                )
            )
    return tuple(observations)


def statistics_module_median(values: list[float]) -> float:
    """Small named seam that keeps source-label aggregation easy to test."""

    return statistics.median(values)


def _selection_metrics(
    observations: tuple[CalibrationObservation, ...],
    selected_by_group: dict[tuple[str, int, str], str],
    *,
    practical_tie_fraction: float,
) -> dict[str, Any]:
    """Evaluate selected plans against a 3%-equivalent legal Oracle set."""

    groups: dict[
        tuple[str, int, str],
        dict[str, float],
    ] = defaultdict(dict)
    for item in observations:
        groups[(item.scenario_id, item.seed, item.equivalence_group)][item.candidate_id] = (
            item.latency_ms
        )
    regrets: list[float] = []
    hits: list[bool] = []
    decisions: list[dict[str, Any]] = []
    for key, actual in sorted(groups.items()):
        selected = selected_by_group[key]
        actual_best = min(actual.values())
        oracle = tuple(
            sorted(
                candidate_id
                for candidate_id, latency in actual.items()
                if latency <= actual_best * (1.0 + practical_tie_fraction)
            )
        )
        regret = (actual[selected] / actual_best - 1.0) * 100.0
        regrets.append(regret)
        hits.append(selected in oracle)
        decisions.append(
            {
                "scenario_id": key[0],
                "seed": key[1],
                "selected_candidate_id": selected,
                "oracle_candidate_ids": list(oracle),
                "oracle_hit": selected in oracle,
                "regret_percent": regret,
            }
        )
    ordered = sorted(regrets)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "decision_count": len(decisions),
        "oracle_set_hit_rate": statistics.mean(hits),
        "mean_regret_percent": statistics.mean(ordered),
        "p95_regret_percent": ordered[p95_index],
        "maximum_regret_percent": max(ordered),
        "selected_candidate_counts": dict(sorted(Counter(selected_by_group.values()).items())),
        "decisions": decisions,
    }


def fixed_candidate_baselines(
    observations: tuple[CalibrationObservation, ...],
    *,
    practical_tie_fraction: float,
) -> dict[str, dict[str, Any]]:
    """Report every fixed route; never hide the strongest simple baseline."""

    keys = {(item.scenario_id, item.seed, item.equivalence_group) for item in observations}
    candidate_ids = sorted({item.candidate_id for item in observations})
    return {
        candidate_id: _selection_metrics(
            observations,
            {key: candidate_id for key in keys},
            practical_tie_fraction=practical_tie_fraction,
        )
        for candidate_id in candidate_ids
    }


def calibrate_governed_pipeline_cost_model(
    config: GovernedPipelineCostCalibrationConfig,
    *,
    project_root: Path,
    progress_callback: Any | None = None,
) -> Path:
    """Run grouped development validation and serialize an auditable model."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Cost calibration requires a clean worktree")
    source = root / config.source_run_dir
    observations = load_governed_pipeline_cost_observations(
        source,
        expected_summary_sha256=config.source_summary_sha256,
    )
    stable_preferences = {EQUIVALENCE_GROUP: config.stable_preference_candidate_id}
    validation = grouped_outer_validation(
        observations,
        lambda_grid=config.lambda_grid,
        stable_preferences=stable_preferences,
        practical_tie_fraction=config.practical_tie_fraction,
        progress_callback=progress_callback,
    )
    selected_counts = Counter(
        str(item["selected_candidate_id"]) for item in validation["decisions"]
    )
    baselines = fixed_candidate_baselines(
        observations,
        practical_tie_fraction=config.practical_tie_fraction,
    )
    best_fixed_id, best_fixed = min(
        baselines.items(),
        key=lambda item: (
            item[1]["mean_regret_percent"],
            item[1]["p95_regret_percent"],
            item[0],
        ),
    )

    selected_lambda = select_lambda_inside_training(
        observations,
        lambda_grid=config.lambda_grid,
    )
    final_fit = fit_nonnegative_analytic_cost(
        observations,
        ridge_lambda=selected_lambda,
    )
    gates = {
        "minimum_oracle_set_hit_rate": (
            validation["oracle_set_hit_rate"] >= config.minimum_oracle_set_hit_rate
        ),
        "maximum_mean_regret_percent": (
            validation["mean_regret_percent"] <= config.maximum_mean_regret_percent
        ),
        "maximum_p95_regret_percent": (
            validation["p95_regret_percent"] <= config.maximum_p95_regret_percent
        ),
        "maximum_regret_percent": (
            validation["maximum_regret_percent"] <= config.maximum_regret_percent
        ),
        "minimum_selected_candidate_count": (
            len(selected_counts) >= config.minimum_selected_candidate_count
        ),
        "not_worse_than_best_fixed_mean": (
            not config.require_not_worse_than_best_fixed_mean
            or validation["mean_regret_percent"] <= best_fixed["mean_regret_percent"]
        ),
        "not_worse_than_best_fixed_p95": (
            not config.require_not_worse_than_best_fixed_p95
            or validation["p95_regret_percent"] <= best_fixed["p95_regret_percent"]
        ),
        "final_fit_converged": final_fit.converged,
    }
    passed = all(gates.values())
    output = root / config.results_dir
    output.mkdir(parents=True, exist_ok=True)
    model = {
        "model_type": "nonnegative-additive-physical-work-v1",
        "source_summary_sha256": config.source_summary_sha256,
        "equivalence_group": EQUIVALENCE_GROUP,
        "intercept_ms": final_fit.intercept_ms,
        "coefficients": dict(final_fit.coefficients),
        "ridge_lambda": final_fit.ridge_lambda,
        "stable_preference_candidate_id": config.stable_preference_candidate_id,
        "practical_tie_fraction": config.practical_tie_fraction,
        "development_status": (
            "AUTHORIZED_FOR_INDEPENDENT_HOLDOUT" if passed else "RETAINED_DEVELOPMENT_FAILURE"
        ),
    }
    _atomic_json(output / "model.json", model)
    result = {
        "status": (
            "PASS_GOVERNED_PIPELINE_COST_MODEL_DEVELOPMENT"
            if passed
            else "FAIL_GOVERNED_PIPELINE_COST_MODEL_DEVELOPMENT_RETAIN"
        ),
        "independent_holdout_authorized": passed,
        "source_run_dir": config.source_run_dir,
        "source_summary_sha256": config.source_summary_sha256,
        "calibration_commit": commit,
        "config": asdict(config),
        "observation_count": len(observations),
        "scenario_count": len({item.scenario_id for item in observations}),
        "grouped_validation": validation,
        "model_selected_candidate_counts": dict(sorted(selected_counts.items())),
        "fixed_baselines": baselines,
        "best_fixed_candidate_id": best_fixed_id,
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "model": model,
        "paper_performance_claim_authorized": false_value(),
    }
    _atomic_json(output / "calibration.json", result)
    return output


def false_value() -> bool:
    """Make the development-only claim boundary conspicuous in source."""

    return False
