"""One-shot evaluation of a frozen governed pipeline cost model."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
)
from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.experiments.governed_pipeline_cost_calibration import (
    EQUIVALENCE_GROUP,
    _selection_metrics,
    fixed_candidate_baselines,
)
from trustaero.optimizer.governed_pipeline_space import (
    GovernedPipelineStatistics,
    build_governed_pipeline_candidates,
)


@dataclass(frozen=True, slots=True)
class GovernedPipelineCostHoldoutConfig:
    """Frozen artifacts, unseen factors, and one-shot acceptance gates."""

    results_dir: str
    measurement_results_dir: str
    model_path: str
    model_sha256: str
    development_calibration_path: str
    development_calibration_sha256: str
    expected_factors: dict[str, object]
    minimum_oracle_set_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_selected_candidate_count: int
    require_not_worse_than_best_fixed_mean: bool
    require_not_worse_than_best_fixed_p95: bool


def load_governed_pipeline_cost_holdout_config(
    path: Path | str,
) -> GovernedPipelineCostHoldoutConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return GovernedPipelineCostHoldoutConfig(
        results_dir=str(payload["results_dir"]),
        measurement_results_dir=str(payload["measurement_results_dir"]),
        model_path=str(payload["model_path"]),
        model_sha256=str(payload["model_sha256"]),
        development_calibration_path=str(payload["development_calibration_path"]),
        development_calibration_sha256=str(payload["development_calibration_sha256"]),
        expected_factors=dict(payload["expected_factors"]),
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
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_holdout_observations(
    run_dir: Path,
) -> tuple[
    tuple[CalibrationObservation, ...],
    dict[tuple[str, int, str], tuple[str, ...]],
    dict[str, object],
]:
    """Rebuild physical work and independently recheck measurement integrity."""

    observations: list[CalibrationObservation] = []
    legal_by_group: dict[tuple[str, int, str], tuple[str, ...]] = {}
    unit_paths = sorted((run_dir / "units").glob("*.json"))
    if not unit_paths:
        raise ValueError("Holdout measurement contains no unit records")
    for path in unit_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        unit = payload["unit"]
        actual = payload["actual_cardinalities"]
        measurements = payload["measurements"]
        if len(measurements) != 90:
            raise ValueError(f"Incomplete holdout timing unit: {path.name}")
        if len({item["result_digest"] for item in measurements}) != 1:
            raise ValueError(f"Holdout result mismatch: {path.name}")
        if len({item["lineage_digest"] for item in measurements}) != 1:
            raise ValueError(f"Holdout lineage mismatch: {path.name}")
        if len(set(payload["plan_fingerprints"].values())) != 3:
            raise ValueError(f"Holdout physical plans collapsed: {path.name}")
        counts = Counter(str(item["candidate_id"]) for item in measurements)
        if set(counts.values()) != {30}:
            raise ValueError(f"Holdout schedule is unbalanced: {path.name}")

        scenario_id = str(measurements[0]["scenario_id"])
        seed = int(unit["seed"])
        statistics_value = GovernedPipelineStatistics(
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
            for candidate in build_governed_pipeline_candidates(statistics_value)
            if candidate.candidate_id in counts
        }
        if set(profiles) != set(counts):
            raise ValueError(f"Holdout candidate profile mismatch: {path.name}")
        group = (scenario_id, seed, EQUIVALENCE_GROUP)
        legal_by_group[group] = tuple(
            str(value) for value in payload["planning"]["nondominated_candidate_ids"]
        )
        for candidate_id, profile in sorted(profiles.items()):
            latencies = [
                float(item["latency_ms"])
                for item in measurements
                if item["candidate_id"] == candidate_id
            ]
            observations.append(
                CalibrationObservation(
                    scenario_id=scenario_id,
                    seed=seed,
                    equivalence_group=EQUIVALENCE_GROUP,
                    candidate_id=candidate_id,
                    latency_ms=statistics.median(latencies),
                    features=profile.work_metrics,
                )
            )
    integrity: dict[str, object] = {
        "unit_count": len(unit_paths),
        "observation_count": len(observations),
        "measurement_row_count": len(observations) * 30,
        "result_equivalence_passed": True,
        "record_lineage_equivalence_passed": True,
        "physical_plan_distinctness_passed": True,
        "balanced_schedule_passed": True,
    }
    return tuple(observations), legal_by_group, integrity


def _select_with_frozen_model(
    observations: tuple[CalibrationObservation, ...],
    model: dict[str, Any],
) -> tuple[
    dict[tuple[str, int, str], str],
    list[dict[str, object]],
]:
    groups: dict[
        tuple[str, int, str],
        list[CalibrationObservation],
    ] = defaultdict(list)
    for item in observations:
        groups[(item.scenario_id, item.seed, item.equivalence_group)].append(item)
    coefficients = {str(name): float(value) for name, value in model["coefficients"].items()}
    intercept = float(model["intercept_ms"])
    tie = float(model["practical_tie_fraction"])
    preferred = str(model["stable_preference_candidate_id"])
    selected: dict[tuple[str, int, str], str] = {}
    predictions: list[dict[str, object]] = []
    for key, candidates in sorted(groups.items()):
        predicted = {
            item.candidate_id: intercept
            + sum(coefficients.get(name, 0.0) * value for name, value in item.features)
            for item in candidates
        }
        best = min(predicted.values())
        equivalent = {
            candidate_id for candidate_id, value in predicted.items() if value <= best * (1.0 + tie)
        }
        choice = (
            preferred
            if preferred in equivalent
            else min(equivalent, key=lambda item: (predicted[item], item))
        )
        selected[key] = choice
        predictions.append(
            {
                "scenario_id": key[0],
                "seed": key[1],
                "predicted_latency_ms": dict(sorted(predicted.items())),
                "predicted_equivalent_candidate_ids": sorted(equivalent),
                "selected_candidate_id": choice,
            }
        )
    return selected, predictions


def evaluate_governed_pipeline_cost_holdout(
    config: GovernedPipelineCostHoldoutConfig,
    *,
    project_root: Path,
    measurement_run_dir: Path,
) -> Path:
    """Evaluate once without fitting, tuning, or changing the frozen model."""

    root = project_root.resolve()
    run_dir = measurement_run_dir.resolve()
    model_path = root / config.model_path
    development_path = root / config.development_calibration_path
    if _sha256(model_path) != config.model_sha256:
        raise ValueError("Frozen holdout model digest changed")
    if _sha256(development_path) != config.development_calibration_sha256:
        raise ValueError("Development calibration digest changed")
    model = json.loads(model_path.read_text(encoding="utf-8"))
    if model.get("development_status") != "AUTHORIZED_FOR_INDEPENDENT_HOLDOUT":
        raise ValueError("Frozen model is not authorized for holdout")

    measured_config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    for name, expected in config.expected_factors.items():
        if measured_config.get(name) != expected:
            raise ValueError(f"Holdout factor changed: {name}")
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if environment.get("git_dirty") is not False:
        raise ValueError("Holdout measurement was not run from a clean commit")
    measurement_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    carryover_passed = bool(
        measurement_summary.get("gate_checks", {}).get(
            "no_material_carryover",
            False,
        )
    )

    observations, legal_by_group, integrity = _load_holdout_observations(run_dir)
    integrity["systematic_carryover_passed"] = carryover_passed
    selected, predictions = _select_with_frozen_model(observations, model)
    illegal = [
        {
            "scenario_id": key[0],
            "seed": key[1],
            "selected_candidate_id": candidate_id,
        }
        for key, candidate_id in selected.items()
        if candidate_id not in legal_by_group[key]
    ]
    metrics = _selection_metrics(
        observations,
        selected,
        practical_tie_fraction=float(model["practical_tie_fraction"]),
    )
    baselines = fixed_candidate_baselines(
        observations,
        practical_tie_fraction=float(model["practical_tie_fraction"]),
    )
    best_fixed_id, best_fixed = min(
        baselines.items(),
        key=lambda item: (
            item[1]["mean_regret_percent"],
            item[1]["p95_regret_percent"],
            item[0],
        ),
    )
    gates = {
        "minimum_oracle_set_hit_rate": (
            metrics["oracle_set_hit_rate"] >= config.minimum_oracle_set_hit_rate
        ),
        "maximum_mean_regret_percent": (
            metrics["mean_regret_percent"] <= config.maximum_mean_regret_percent
        ),
        "maximum_p95_regret_percent": (
            metrics["p95_regret_percent"] <= config.maximum_p95_regret_percent
        ),
        "maximum_regret_percent": (
            metrics["maximum_regret_percent"] <= config.maximum_regret_percent
        ),
        "minimum_selected_candidate_count": (
            len(metrics["selected_candidate_counts"]) >= config.minimum_selected_candidate_count
        ),
        "not_worse_than_best_fixed_mean": (
            not config.require_not_worse_than_best_fixed_mean
            or metrics["mean_regret_percent"] <= best_fixed["mean_regret_percent"]
        ),
        "not_worse_than_best_fixed_p95": (
            not config.require_not_worse_than_best_fixed_p95
            or metrics["p95_regret_percent"] <= best_fixed["p95_regret_percent"]
        ),
        "zero_illegal_selections": not illegal,
        "no_systematic_material_carryover": carryover_passed,
    }
    passed = all(gates.values())
    output = root / config.results_dir / run_dir.name
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "status": (
            "PASS_GOVERNED_PIPELINE_COST_MODEL_INDEPENDENT_HOLDOUT"
            if passed
            else "FAIL_GOVERNED_PIPELINE_COST_MODEL_INDEPENDENT_HOLDOUT_RETAIN"
        ),
        "model_frozen_before_measurement": True,
        "model_refit_or_threshold_change": False,
        "measurement_run_dir": str(run_dir.relative_to(root)),
        "measurement_summary_sha256": _sha256(run_dir / "summary.json"),
        "model_sha256": config.model_sha256,
        "development_calibration_sha256": (config.development_calibration_sha256),
        "integrity": integrity,
        "holdout_metrics": metrics,
        "fixed_baselines": baselines,
        "best_fixed_candidate_id": best_fixed_id,
        "illegal_selections": illegal,
        "predictions": predictions,
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "paper_performance_claim_authorized": False,
    }
    _atomic_json(output / "evaluation.json", result)
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_dir.name},
    )
    return output
