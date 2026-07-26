"""Grouped real-data calibration for the governed-checkpoint cost model.

Every outer fold removes a complete dataset-month group.  The analytic model
is compared with a query-selectivity threshold learned only inside the same
training fold, so repeated seeds and profiles from a held-out month cannot leak
into either method.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
    select_lambda_inside_training,
)
from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.governed_checkpoint_optimizer_calibration import (
    EQUIVALENCE_GROUP,
    _confidence_oracles,
    _model_from_fit,
    _p95,
    _sha256,
)
from trustaero.experiments.real_governed_checkpoint_transfer import (
    _real_statistics_and_medians,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.execution_aware import AnalyticExecutionCostModel
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
    derive_governed_checkpoint_work,
    rank_governed_checkpoint_candidates,
)


@dataclass(frozen=True, slots=True)
class RealCalibrationSource:
    """One hash-bound, already-consumed real development run."""

    run_dir: str
    expected_status: str
    expected_summary_sha256: str
    expected_measurements_sha256: str


@dataclass(frozen=True, slots=True)
class RealCheckpointCalibrationConfig:
    """Frozen source bindings, grouped validation, and development gates."""

    results_dir: str
    sources: tuple[RealCalibrationSource, ...]
    lambda_grid: tuple[float, ...]
    threshold_grid: tuple[float, ...]
    practical_tie_fraction: float
    support_relative_margin: float
    minimum_confidence_family_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    require_no_worse_family_hit_than_threshold: bool
    require_no_worse_mean_regret_than_threshold: bool
    require_seed_consistency: bool
    require_clean_git: bool

    def __post_init__(self) -> None:
        if len(self.sources) < 3 or len({source.run_dir for source in self.sources}) != len(
            self.sources
        ):
            raise ValueError("Real calibration requires unique source runs")
        if not self.lambda_grid or any(value < 0.0 for value in self.lambda_grid):
            raise ValueError("Real calibration lambda grid is invalid")
        if (
            not self.threshold_grid
            or tuple(sorted(self.threshold_grid)) != self.threshold_grid
            or any(not 0.0 < value < 1.0 for value in self.threshold_grid)
        ):
            raise ValueError("Real calibration threshold grid is invalid")
        if not 0.0 <= self.support_relative_margin < 0.5:
            raise ValueError("Real calibration support margin is invalid")


def load_real_checkpoint_calibration_config(
    path: str | Path,
) -> RealCheckpointCalibrationConfig:
    """Load the hash-bound V4 development protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["sources"] = tuple(RealCalibrationSource(**source) for source in payload["sources"])
    payload["lambda_grid"] = tuple(float(value) for value in payload["lambda_grid"])
    payload["threshold_grid"] = tuple(float(value) for value in payload["threshold_grid"])
    return RealCheckpointCalibrationConfig(**payload)


def source_month_group(scenario_id: str) -> str:
    """Return dataset-month while rejecting malformed scenario identifiers."""

    parts = scenario_id.split("-")
    if len(parts) < 4 or parts[0] not in {"bts", "nyc_tlc"}:
        raise ValueError(f"Unknown real checkpoint scenario ID: {scenario_id}")
    if len(parts[1]) != 4 or len(parts[2]) != 2:
        raise ValueError(f"Scenario ID lacks a year-month: {scenario_id}")
    return "-".join(parts[:3])


def _load_real_development_data(
    root: Path,
    config: RealCheckpointCalibrationConfig,
) -> tuple[
    tuple[CalibrationObservation, ...],
    dict[tuple[str, int], GovernedCheckpointStatistics],
    dict[tuple[str, int, str], float],
    dict[str, tuple[str, ...]],
    dict[str, dict[str, str]],
]:
    observations: list[CalibrationObservation] = []
    statistics_by_key: dict[tuple[str, int], GovernedCheckpointStatistics] = {}
    medians: dict[tuple[str, int, str], float] = {}
    oracles: dict[str, tuple[str, ...]] = {}
    hashes: dict[str, dict[str, str]] = {}
    for source in config.sources:
        run_dir = root / source.run_dir
        summary_path = run_dir / "summary.json"
        measurements_path = run_dir / "measurements.csv"
        actual_hashes = {
            "summary": _sha256(summary_path),
            "measurements": _sha256(measurements_path),
        }
        if actual_hashes != {
            "summary": source.expected_summary_sha256,
            "measurements": source.expected_measurements_sha256,
        }:
            raise ValueError(f"Real calibration source hash mismatch: {source.run_dir}")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("status") != source.expected_status:
            raise ValueError(f"Real calibration source status changed: {source.run_dir}")
        run_oracles = _confidence_oracles(summary)
        run_statistics, run_medians = _real_statistics_and_medians(run_dir)
        if set(oracles) & set(run_oracles):
            raise ValueError("Real calibration source scenarios overlap")
        if set(statistics_by_key) & set(run_statistics):
            raise ValueError("Real calibration source units overlap")
        oracles.update(run_oracles)
        statistics_by_key.update(run_statistics)
        medians.update(run_medians)
        hashes[source.run_dir] = actual_hashes
        for (scenario_id, seed), planner_statistics in run_statistics.items():
            for candidate_id in (
                POLICY_FIRST_CHECKPOINT,
                QUERY_FIRST_CHECKPOINT,
            ):
                observations.append(
                    CalibrationObservation(
                        scenario_id=scenario_id,
                        seed=seed,
                        equivalence_group=EQUIVALENCE_GROUP,
                        candidate_id=candidate_id,
                        latency_ms=run_medians[(scenario_id, seed, candidate_id)],
                        features=derive_governed_checkpoint_work(
                            planner_statistics, candidate_id
                        ).features,
                    )
                )
    if set(oracles) != {item.scenario_id for item in observations}:
        raise ValueError("Real calibration Oracle and observation families differ")
    return tuple(observations), statistics_by_key, medians, oracles, hashes


def _learn_query_threshold(
    training_scenarios: set[str],
    statistics_by_key: Mapping[tuple[str, int], GovernedCheckpointStatistics],
    oracles: Mapping[str, tuple[str, ...]],
    threshold_grid: Sequence[float],
) -> float:
    """Fit the strongest monotone query-selectivity threshold in training."""

    scenario_statistics = {
        scenario_id: statistics_value
        for (scenario_id, _seed), statistics_value in statistics_by_key.items()
        if scenario_id in training_scenarios
    }
    scores: list[tuple[float, float, float]] = []
    for threshold in threshold_grid:
        hits = []
        for scenario_id, planner_statistics in scenario_statistics.items():
            query_rate = planner_statistics.estimated_query_rows / planner_statistics.input_rows
            selected = QUERY_FIRST_CHECKPOINT if query_rate < threshold else POLICY_FIRST_CHECKPOINT
            hits.append(selected in oracles[scenario_id])
        # Prefer the smaller threshold on an exact hit-rate tie. This is a
        # deterministic conservative rule, not a held-out-data choice.
        scores.append((statistics.mean(hits), -threshold, threshold))
    return max(scores)[2]


def _metrics(
    decisions: Sequence[Mapping[str, object]],
    *,
    selection_field: str,
    regret_field: str,
    oracles: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    family_selections: dict[str, set[str]] = defaultdict(set)
    regrets: list[float] = []
    for row in decisions:
        scenario_id = str(row["scenario_id"])
        family_selections[scenario_id].add(str(row[selection_field]))
        regrets.append(float(cast(float, row[regret_field])))
    family_hits = [
        selections.issubset(set(oracles[scenario_id]))
        for scenario_id, selections in family_selections.items()
    ]
    return {
        "confidence_family_hit_rate": statistics.mean(family_hits),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _p95(regrets),
        "max_regret_percent": max(regrets),
        "seed_consistent_family_rate": statistics.mean(
            len(values) == 1 for values in family_selections.values()
        ),
    }


def calibrate_real_checkpoint_optimizer(
    config: RealCheckpointCalibrationConfig,
    *,
    project_root: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Grouped-validate and fit the real-calibrated analytic V4 model."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Real V4 calibration requires a clean Git commit")
    (
        observations,
        statistics_by_key,
        medians,
        oracles,
        source_hashes,
    ) = _load_real_development_data(root, config)
    groups = tuple(sorted({source_month_group(item.scenario_id) for item in observations}))
    decisions: list[dict[str, object]] = []
    folds: list[dict[str, object]] = []
    permissive = GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None)
    for fold_index, heldout_group in enumerate(groups, start=1):
        training = tuple(
            item for item in observations if source_month_group(item.scenario_id) != heldout_group
        )
        testing_keys = tuple(
            key for key in statistics_by_key if source_month_group(key[0]) == heldout_group
        )
        training_scenarios = {item.scenario_id for item in training}
        selected_lambda = select_lambda_inside_training(training, lambda_grid=config.lambda_grid)
        model = _model_from_fit(
            training,
            ridge_lambda=selected_lambda,
            calibration_id=f"real-v4-lomo:{heldout_group}",
            practical_tie_fraction=config.practical_tie_fraction,
            support_relative_margin=config.support_relative_margin,
        )
        learned_threshold = _learn_query_threshold(
            training_scenarios,
            statistics_by_key,
            oracles,
            config.threshold_grid,
        )
        for scenario_id, seed in testing_keys:
            planner_statistics = statistics_by_key[(scenario_id, seed)]
            ranking = rank_governed_checkpoint_candidates(planner_statistics, permissive, model)
            analytic_selected = ranking.selected_candidate_id
            if analytic_selected is None:
                raise ValueError("Real V4 permissive fold rejected every candidate")
            query_rate = planner_statistics.estimated_query_rows / planner_statistics.input_rows
            threshold_selected = (
                QUERY_FIRST_CHECKPOINT
                if query_rate < learned_threshold
                else POLICY_FIRST_CHECKPOINT
            )
            actual = {
                candidate_id: medians[(scenario_id, seed, candidate_id)]
                for candidate_id in (
                    POLICY_FIRST_CHECKPOINT,
                    QUERY_FIRST_CHECKPOINT,
                )
            }
            best = min(actual.values())
            decisions.append(
                {
                    "scenario_id": scenario_id,
                    "source_month_group": heldout_group,
                    "seed": seed,
                    "analytic_selected_candidate_id": analytic_selected,
                    "analytic_reason_code": ranking.reason_code,
                    "analytic_regret_percent": (actual[analytic_selected] / best - 1.0) * 100.0,
                    "threshold_selected_candidate_id": threshold_selected,
                    "threshold_regret_percent": (actual[threshold_selected] / best - 1.0) * 100.0,
                    "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                    "estimated_costs_ms": {
                        estimate.candidate_id: estimate.total_ms for estimate in ranking.estimates
                    },
                }
            )
        folds.append(
            {
                "heldout_source_month_group": heldout_group,
                "training_source_month_group_count": len(groups) - 1,
                "selected_ridge_lambda": selected_lambda,
                "learned_query_selectivity_threshold": learned_threshold,
            }
        )
        if progress_callback is not None:
            progress_callback(fold_index, len(groups), heldout_group)

    analytic_metrics = _metrics(
        decisions,
        selection_field="analytic_selected_candidate_id",
        regret_field="analytic_regret_percent",
        oracles=oracles,
    )
    threshold_metrics = _metrics(
        decisions,
        selection_field="threshold_selected_candidate_id",
        regret_field="threshold_regret_percent",
        oracles=oracles,
    )
    fixed_metrics = {}
    for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT):
        fixed_decisions = [
            {
                **row,
                "fixed_selected_candidate_id": candidate_id,
                "fixed_regret_percent": (
                    medians[
                        (
                            str(row["scenario_id"]),
                            int(cast(int, row["seed"])),
                            candidate_id,
                        )
                    ]
                    / min(
                        medians[
                            (
                                str(row["scenario_id"]),
                                int(cast(int, row["seed"])),
                                candidate,
                            )
                        ]
                        for candidate in (
                            POLICY_FIRST_CHECKPOINT,
                            QUERY_FIRST_CHECKPOINT,
                        )
                    )
                    - 1.0
                )
                * 100.0,
            }
            for row in decisions
        ]
        fixed_metrics[candidate_id] = _metrics(
            fixed_decisions,
            selection_field="fixed_selected_candidate_id",
            regret_field="fixed_regret_percent",
            oracles=oracles,
        )

    final_lambda = select_lambda_inside_training(observations, lambda_grid=config.lambda_grid)
    final_model: AnalyticExecutionCostModel = _model_from_fit(
        observations,
        ridge_lambda=final_lambda,
        calibration_id="real-governed-checkpoint-v4-development",
        practical_tie_fraction=config.practical_tie_fraction,
        support_relative_margin=config.support_relative_margin,
    )
    final_threshold = _learn_query_threshold(
        set(oracles), statistics_by_key, oracles, config.threshold_grid
    )
    strict = GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0)
    strict_violations = sum(
        rank_governed_checkpoint_candidates(
            planner_statistics, strict, final_model
        ).selected_candidate_id
        != POLICY_FIRST_CHECKPOINT
        for planner_statistics in statistics_by_key.values()
    )
    gates = {
        "minimum_confidence_family_hit_rate": float(
            cast(float, analytic_metrics["confidence_family_hit_rate"])
        )
        >= config.minimum_confidence_family_hit_rate,
        "maximum_mean_regret_percent": float(cast(float, analytic_metrics["mean_regret_percent"]))
        <= config.maximum_mean_regret_percent,
        "maximum_p95_regret_percent": float(cast(float, analytic_metrics["p95_regret_percent"]))
        <= config.maximum_p95_regret_percent,
        "maximum_regret_percent": float(cast(float, analytic_metrics["max_regret_percent"]))
        <= config.maximum_regret_percent,
        "no_worse_family_hit_than_threshold": (
            float(cast(float, analytic_metrics["confidence_family_hit_rate"]))
            >= float(cast(float, threshold_metrics["confidence_family_hit_rate"]))
            or not config.require_no_worse_family_hit_than_threshold
        ),
        "no_worse_mean_regret_than_threshold": (
            float(cast(float, analytic_metrics["mean_regret_percent"]))
            <= float(cast(float, threshold_metrics["mean_regret_percent"])) + 1e-12
            or not config.require_no_worse_mean_regret_than_threshold
        ),
        "seed_consistency": (
            float(cast(float, analytic_metrics["seed_consistent_family_rate"])) == 1.0
            or not config.require_seed_consistency
        ),
        "governance_legality": strict_violations == 0,
    }
    passed = all(gates.values())
    result = {
        "status": (
            "PASS_REAL_CHECKPOINT_OPTIMIZER_V4_DEVELOPMENT"
            if passed
            else "FAIL_REAL_CHECKPOINT_OPTIMIZER_V4_DEVELOPMENT_RETAIN"
        ),
        "analysis_git_commit": commit,
        "analysis_git_dirty": dirty,
        "source_hashes": source_hashes,
        "source_month_group_count": len(groups),
        "scenario_count": len(oracles),
        "unit_count": len(statistics_by_key),
        "outer_validation": {
            "method": "leave_one_complete_dataset_month_out",
            "folds": folds,
            "decisions": decisions,
        },
        "analytic_metrics": analytic_metrics,
        "learned_threshold_metrics": threshold_metrics,
        "fixed_metrics": fixed_metrics,
        "final_selected_ridge_lambda": final_lambda,
        "final_learned_query_selectivity_threshold": final_threshold,
        "final_model": final_model.to_dict(),
        "strict_policy_illegal_selection_count": strict_violations,
        "gates": gates,
        "validation_claim_authorized": False,
        "final_holdout_claim_authorized": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This result uses consumed real development months only. Passing "
            "authorizes freezing V4 for a separate month-level validation, not a "
            "paper performance claim."
        ),
    }
    output_root = root / config.results_dir
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "config.json", asdict(config))
    _atomic_json(output_dir / "model.json", final_model.to_dict())
    _atomic_json(output_dir / "calibration.json", result)
    _atomic_json(output_root / "latest_run.json", {"run_id": run_id})
    return output_dir
