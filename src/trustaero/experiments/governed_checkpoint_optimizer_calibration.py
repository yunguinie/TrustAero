"""Grouped development calibration for the EA-1 checkpoint optimizer.

The eight reversal-discovery scenarios are consumed development data. Each
outer fold removes a complete rows-width-policy-query family, including all of
its seeds. The primary Oracle labels come from the frozen paired confidence
intervals, not unstable per-seed winners.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
    fit_nonnegative_analytic_cost,
    select_lambda_inside_training,
)
from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.execution_aware import (
    AnalyticExecutionCostModel,
    AnalyticFeatureRate,
    FeatureSupportBound,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
    derive_governed_checkpoint_work,
    rank_governed_checkpoint_candidates,
)

EQUIVALENCE_GROUP = "governed_checkpoint"


@dataclass(frozen=True, slots=True)
class CheckpointCalibrationConfig:
    """Frozen source bindings, hyperparameters, and development gates."""

    source_run_dir: str
    results_dir: str
    lambda_grid: tuple[float, ...]
    practical_tie_fraction: float
    expected_summary_sha256: str
    expected_measurements_sha256: str
    require_clean_git: bool
    minimum_confidence_family_hit_rate: float
    maximum_mean_diagnostic_regret_percent: float
    maximum_diagnostic_regret_percent: float
    require_better_than_both_fixed: bool
    require_seed_consistent_selection: bool
    support_relative_margin: float

    def __post_init__(self) -> None:
        if not self.lambda_grid or any(value < 0.0 for value in self.lambda_grid):
            raise ValueError("Checkpoint calibration lambda grid is invalid")
        if len(self.lambda_grid) != len(set(self.lambda_grid)):
            raise ValueError("Checkpoint calibration lambdas must be unique")
        fractions = (
            self.practical_tie_fraction,
            self.minimum_confidence_family_hit_rate,
        )
        if any(not 0.0 <= value <= 1.0 for value in fractions):
            raise ValueError("Checkpoint calibration fractions are invalid")
        if (
            min(
                self.maximum_mean_diagnostic_regret_percent,
                self.maximum_diagnostic_regret_percent,
            )
            < 0.0
        ):
            raise ValueError("Checkpoint calibration regret gates are invalid")
        if not 0.0 <= self.support_relative_margin < 0.5:
            raise ValueError("Checkpoint calibration support margin is invalid")
        if any(
            len(value) != 64
            for value in (
                self.expected_summary_sha256,
                self.expected_measurements_sha256,
            )
        ):
            raise ValueError("Checkpoint calibration hashes must be SHA-256")


def load_checkpoint_calibration_config(
    path: str | Path,
) -> CheckpointCalibrationConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return CheckpointCalibrationConfig(
        source_run_dir=str(payload["source_run_dir"]),
        results_dir=str(payload["results_dir"]),
        lambda_grid=tuple(float(value) for value in payload["lambda_grid"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        expected_summary_sha256=str(payload["expected_summary_sha256"]),
        expected_measurements_sha256=str(payload["expected_measurements_sha256"]),
        require_clean_git=bool(payload["require_clean_git"]),
        minimum_confidence_family_hit_rate=float(payload["minimum_confidence_family_hit_rate"]),
        maximum_mean_diagnostic_regret_percent=float(
            payload["maximum_mean_diagnostic_regret_percent"]
        ),
        maximum_diagnostic_regret_percent=float(payload["maximum_diagnostic_regret_percent"]),
        require_better_than_both_fixed=bool(payload["require_better_than_both_fixed"]),
        require_seed_consistent_selection=bool(payload["require_seed_consistent_selection"]),
        support_relative_margin=float(payload["support_relative_margin"]),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _confidence_oracles(summary: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for row in cast(list[dict[str, Any]], summary["scenario_results"]):
        conclusion = str(row["conclusion"])
        oracle: tuple[str, ...]
        if conclusion == "LEFT_MATERIALLY_FASTER":
            oracle = (POLICY_FIRST_CHECKPOINT,)
        elif conclusion == "LEFT_MATERIALLY_SLOWER":
            oracle = (QUERY_FIRST_CHECKPOINT,)
        elif conclusion == "NO_PRACTICAL_DOMINANCE_AUTHORIZED":
            oracle = (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
        else:
            raise ValueError(f"Unknown EA-1 confidence conclusion: {conclusion}")
        result[str(row["scenario_id"])] = oracle
    return result


def _load_development_data(
    run_dir: Path,
) -> tuple[
    tuple[CalibrationObservation, ...],
    dict[tuple[str, int], GovernedCheckpointStatistics],
    dict[str, tuple[str, ...]],
]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY":
        raise ValueError("Checkpoint calibration requires a passed EA-1 run")
    if summary.get("reversal_discovery") != "STABLE_BIDIRECTIONAL_REVERSAL_DISCOVERED":
        raise ValueError("Checkpoint calibration requires bidirectional reversals")
    oracles = _confidence_oracles(summary)
    statistics_by_key: dict[tuple[str, int], GovernedCheckpointStatistics] = {}
    for path in sorted((run_dir / "units").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        unit = cast(dict[str, Any], payload["unit"])
        actual = cast(dict[str, Any], payload["actual_cardinalities"])
        seed = int(unit["seed"])
        unit_id = str(payload["unit_id"])
        seed_suffix = f"-s{seed}"
        if not unit_id.endswith(seed_suffix):
            raise ValueError(f"EA-1 unit ID is not bound to its seed: {unit_id}")
        key = (unit_id[: -len(seed_suffix)], seed)
        if key in statistics_by_key:
            raise ValueError(f"Duplicate EA-1 unit statistics: {key}")
        statistics_by_key[key] = GovernedCheckpointStatistics(
            input_rows=int(unit["row_count"]),
            sensitive_width_bytes=float(unit["identifier_width"]),
            estimated_policy_rows=int(actual["policy_rows"]),
            estimated_query_rows=int(actual["query_rows"]),
            estimated_result_rows=int(actual["result_rows"]),
            statistic_provenance="catalog_exact_controlled",
        )

    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    latencies: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    for row in rows:
        latencies[(row["scenario_id"], int(row["seed"]), row["candidate_id"])].append(
            float(row["latency_ms"])
        )
    observations: list[CalibrationObservation] = []
    for (scenario_id, seed, candidate_id), values in sorted(latencies.items()):
        statistics = statistics_by_key[(scenario_id, seed)]
        work = derive_governed_checkpoint_work(statistics, candidate_id)
        observations.append(
            CalibrationObservation(
                scenario_id=scenario_id,
                seed=seed,
                equivalence_group=EQUIVALENCE_GROUP,
                candidate_id=candidate_id,
                latency_ms=statistics_module_median(values),
                features=work.features,
            )
        )
    if set(oracles) != {item.scenario_id for item in observations}:
        raise ValueError("EA-1 confidence Oracle families do not match observations")
    return tuple(observations), statistics_by_key, oracles


def statistics_module_median(values: Sequence[float]) -> float:
    """Named wrapper keeps latency aggregation explicit in serialized methods."""

    return statistics.median(values)


def _model_from_fit(
    observations: tuple[CalibrationObservation, ...],
    *,
    ridge_lambda: float,
    calibration_id: str,
    practical_tie_fraction: float,
    support_relative_margin: float,
) -> AnalyticExecutionCostModel:
    fit = fit_nonnegative_analytic_cost(
        observations,
        ridge_lambda=ridge_lambda,
        maximum_iterations=10_000,
        tolerance=1e-8,
    )
    if not fit.converged:
        raise ValueError(f"Governed-checkpoint analytic fit did not converge: {calibration_id}")
    feature_names = tuple(name for name, _value in fit.coefficients)
    values = [dict(item.features) for item in observations]
    bounds: list[FeatureSupportBound] = []
    for name in feature_names:
        minimum = min(row.get(name, 0.0) for row in values)
        maximum = max(row.get(name, 0.0) for row in values)
        # Catalog cardinalities and selectivity estimates are not exact. A
        # frozen relative envelope prevents harmless seed-level count jitter
        # from becoming a false OOD event while still rejecting new scales.
        margin = maximum * support_relative_margin
        bounds.append(
            FeatureSupportBound(
                name,
                max(0.0, minimum - margin),
                maximum + margin,
            )
        )
    return AnalyticExecutionCostModel(
        calibration_id=calibration_id,
        rates=tuple(
            AnalyticFeatureRate(name, coefficient) for name, coefficient in fit.coefficients
        ),
        support_bounds=tuple(bounds),
        stable_legal_preference=(
            POLICY_FIRST_CHECKPOINT,
            QUERY_FIRST_CHECKPOINT,
        ),
        practical_tie_fraction=practical_tie_fraction,
        intercept_ms=fit.intercept_ms,
    )


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _fixed_family_hit_rate(oracles: Mapping[str, tuple[str, ...]], candidate_id: str) -> float:
    return statistics.mean(candidate_id in values for values in oracles.values())


def calibrate_governed_checkpoint_optimizer(
    config: CheckpointCalibrationConfig,
    *,
    project_root: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Run grouped development validation and serialize the final analytic model."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Checkpoint optimizer calibration requires a clean Git commit")
    run_dir = root / config.source_run_dir
    actual_hashes = {
        "summary": _sha256(run_dir / "summary.json"),
        "measurements": _sha256(run_dir / "measurements.csv"),
    }
    expected_hashes = {
        "summary": config.expected_summary_sha256,
        "measurements": config.expected_measurements_sha256,
    }
    if actual_hashes != expected_hashes:
        raise ValueError("Checkpoint optimizer calibration source hash mismatch")
    observations, statistics_by_key, oracles = _load_development_data(run_dir)
    scenarios = tuple(sorted(oracles))
    decisions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    for fold_index, scenario_id in enumerate(scenarios, start=1):
        training = tuple(item for item in observations if item.scenario_id != scenario_id)
        testing = tuple(item for item in observations if item.scenario_id == scenario_id)
        selected_lambda = select_lambda_inside_training(training, lambda_grid=config.lambda_grid)
        model = _model_from_fit(
            training,
            ridge_lambda=selected_lambda,
            calibration_id=f"ea1-lofo:{scenario_id}",
            practical_tie_fraction=config.practical_tie_fraction,
            support_relative_margin=config.support_relative_margin,
        )
        labels = {(item.seed, item.candidate_id): item.latency_ms for item in testing}
        for seed in sorted({item.seed for item in testing}):
            ranking = rank_governed_checkpoint_candidates(
                statistics_by_key[(scenario_id, seed)],
                GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None),
                model,
            )
            selected = ranking.selected_candidate_id
            if selected is None:
                raise ValueError("Permissive EA-1 ranking rejected every candidate")
            actual = {
                candidate_id: labels[(seed, candidate_id)]
                for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
            }
            best = min(actual.values())
            decisions.append(
                {
                    "scenario_id": scenario_id,
                    "seed": seed,
                    "selected_candidate_id": selected,
                    "reason_code": ranking.reason_code,
                    "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                    "confidence_oracle_hit": selected in oracles[scenario_id],
                    "diagnostic_median_regret_percent": (actual[selected] / best - 1.0) * 100.0,
                    "estimated_costs_ms": {
                        estimate.candidate_id: estimate.total_ms for estimate in ranking.estimates
                    },
                }
            )
        folds.append(
            {
                "held_out_scenario_id": scenario_id,
                "training_scenario_count": len(scenarios) - 1,
                "selected_ridge_lambda": selected_lambda,
            }
        )
        if progress_callback is not None:
            progress_callback(fold_index, len(scenarios), scenario_id)

    family_results: list[dict[str, Any]] = []
    for scenario_id in scenarios:
        family_selections = {
            str(row["selected_candidate_id"])
            for row in decisions
            if row["scenario_id"] == scenario_id
        }
        family_results.append(
            {
                "scenario_id": scenario_id,
                "selected_candidate_ids_across_seeds": sorted(family_selections),
                "seed_consistent": len(family_selections) == 1,
                "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                "confidence_oracle_hit": family_selections.issubset(set(oracles[scenario_id])),
            }
        )
    regrets = [float(row["diagnostic_median_regret_percent"]) for row in decisions]
    family_hit_rate = statistics.mean(bool(row["confidence_oracle_hit"]) for row in family_results)
    seed_consistency_rate = statistics.mean(bool(row["seed_consistent"]) for row in family_results)
    fixed_policy_hit = _fixed_family_hit_rate(oracles, POLICY_FIRST_CHECKPOINT)
    fixed_query_hit = _fixed_family_hit_rate(oracles, QUERY_FIRST_CHECKPOINT)
    best_fixed_hit = max(fixed_policy_hit, fixed_query_hit)

    final_lambda = select_lambda_inside_training(observations, lambda_grid=config.lambda_grid)
    final_model = _model_from_fit(
        observations,
        ridge_lambda=final_lambda,
        calibration_id="ea1-governed-checkpoint-development-v1",
        practical_tie_fraction=config.practical_tie_fraction,
        support_relative_margin=config.support_relative_margin,
    )
    strict_violations = 0
    for statistics_value in statistics_by_key.values():
        strict = rank_governed_checkpoint_candidates(
            statistics_value,
            GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0),
            final_model,
        )
        strict_violations += strict.selected_candidate_id != POLICY_FIRST_CHECKPOINT

    metrics = {
        "confidence_family_hit_rate": family_hit_rate,
        "seed_decision_confidence_hit_rate": statistics.mean(
            bool(row["confidence_oracle_hit"]) for row in decisions
        ),
        "seed_consistent_family_rate": seed_consistency_rate,
        "mean_diagnostic_median_regret_percent": statistics.mean(regrets),
        "p95_diagnostic_median_regret_percent": _p95(regrets),
        "maximum_diagnostic_median_regret_percent": max(regrets),
        "fixed_policy_first_confidence_family_hit_rate": fixed_policy_hit,
        "fixed_query_first_confidence_family_hit_rate": fixed_query_hit,
        "best_fixed_confidence_family_hit_rate": best_fixed_hit,
        "strict_policy_illegal_selection_count": strict_violations,
    }
    gates = {
        "minimum_confidence_family_hit_rate": (
            family_hit_rate >= config.minimum_confidence_family_hit_rate
        ),
        "better_than_both_fixed": (
            family_hit_rate > best_fixed_hit if config.require_better_than_both_fixed else True
        ),
        "seed_consistent_selection": (
            seed_consistency_rate == 1.0 if config.require_seed_consistent_selection else True
        ),
        "maximum_mean_diagnostic_regret": (
            metrics["mean_diagnostic_median_regret_percent"]
            <= config.maximum_mean_diagnostic_regret_percent
        ),
        "maximum_diagnostic_regret": (
            metrics["maximum_diagnostic_median_regret_percent"]
            <= config.maximum_diagnostic_regret_percent
        ),
        "zero_illegal_selections": strict_violations == 0,
    }
    status = (
        "PASS_EA1_CHECKPOINT_OPTIMIZER_DEVELOPMENT"
        if all(gates.values())
        else "FAIL_EA1_CHECKPOINT_OPTIMIZER_RETAIN"
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": status,
        "analysis_commit_hash": commit,
        "analysis_git_dirty": dirty,
        "source_hashes": actual_hashes,
        "source_role": "consumed development reversal-discovery matrix",
        "metrics": metrics,
        "gate_checks": gates,
        "outer_folds": folds,
        "family_results": family_results,
        "decisions": decisions,
        "final_selected_ridge_lambda": final_lambda,
        "final_model": final_model.to_dict(),
        "governance_before_cost": True,
        "direct_winner_classifier_used": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This grouped validation develops an analytic optimizer on consumed EA-1 "
            "scenarios. Passing authorizes freezing a model for a new holdout; it is "
            "not itself independent paper performance evidence."
        ),
        "config": asdict(config),
    }
    _atomic_json(output_dir / "calibration.json", result)
    _atomic_json(output_dir / "model.json", final_model.to_dict())
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})
    return output_dir
