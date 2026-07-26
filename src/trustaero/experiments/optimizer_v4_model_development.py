"""Grouped development evaluation for Pipeline-aware Optimizer V4."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_data_pilot import _git_state
from trustaero.experiments.real_optimizer_transfer import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    _load_frozen_models,
    load_real_optimizer_transfer_config,
)
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures, choose_mask_placement
from trustaero.optimizer.mask_interaction import (
    choose_mask_placement_by_stable_interaction_cost,
)
from trustaero.optimizer.mask_pipeline_v4 import RealPipelineWorkloadStats
from trustaero.optimizer.mask_pipeline_v4_model import (
    PipelineV4CostModel,
    choose_mask_placement_v4,
    pipeline_v4_model_feature_vector,
)
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class V4DevelopmentGates:
    minimum_within_3_percent_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_direct_coverage: float
    minimum_unstable_uncertainty_capture: float
    maximum_mean_regret_gap_vs_match_baseline: float
    maximum_p95_regret_gap_vs_match_baseline: float


@dataclass(frozen=True, slots=True)
class V4ModelDevelopmentConfig:
    protocol_name: str
    results_dir: str
    calibration_run_dir: str
    audit_path: str
    audit_sha256: str
    v3_transfer_config_path: str
    ridge_lambda: float
    uncertainty_residual_quantile: float
    tie_threshold_fraction: float
    require_clean_git: bool
    gates: V4DevelopmentGates
    scientific_boundary: str

    def __post_init__(self) -> None:
        if self.ridge_lambda <= 0.0:
            raise ValueError("V4 development ridge must be positive")
        if not 0.0 <= self.uncertainty_residual_quantile <= 1.0:
            raise ValueError("V4 uncertainty quantile must be in [0, 1]")
        if not 0.0 <= self.tie_threshold_fraction < 1.0:
            raise ValueError("V4 tie threshold must be a fraction")


@dataclass(frozen=True, slots=True)
class V4Observation:
    family_id: str
    scenario_group: str
    stats: RealPipelineWorkloadStats
    paired_ratio: float
    stable: bool

    @property
    def actual_direction(self) -> str:
        return EARLY_CANDIDATE if self.paired_ratio < 1.0 else LATE_CANDIDATE


@dataclass(frozen=True, slots=True)
class _LinearModel:
    intercept: float
    coefficients: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def predict(self, values: Sequence[float]) -> float:
        return self.intercept + sum(
            coefficient * ((value - mean) / scale)
            for coefficient, value, mean, scale in zip(
                self.coefficients, values, self.means, self.scales, strict=True
            )
        )


def load_v4_model_development_config(path: Path | str) -> V4ModelDevelopmentConfig:
    payload = _load_json(Path(path))
    gates = cast(dict[str, Any], payload["gates"])
    return V4ModelDevelopmentConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        calibration_run_dir=str(payload["calibration_run_dir"]),
        audit_path=str(payload["audit_path"]),
        audit_sha256=str(payload["audit_sha256"]),
        v3_transfer_config_path=str(payload["v3_transfer_config_path"]),
        ridge_lambda=float(payload["ridge_lambda"]),
        uncertainty_residual_quantile=float(payload["uncertainty_residual_quantile"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
        gates=V4DevelopmentGates(**gates),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _solve_linear_system(matrix: list[list[float]], target: list[float]) -> list[float]:
    size = len(target)
    augmented = [row[:] + [value] for row, value in zip(matrix, target, strict=True)]
    for pivot in range(size):
        swap = max(range(pivot, size), key=lambda row: abs(augmented[row][pivot]))
        augmented[pivot], augmented[swap] = augmented[swap], augmented[pivot]
        divisor = augmented[pivot][pivot]
        if abs(divisor) < 1e-12:
            raise ValueError("V4 ridge system is singular")
        augmented[pivot] = [item / divisor for item in augmented[pivot]]
        for row in range(size):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            augmented[row] = [
                value - factor * base
                for value, base in zip(augmented[row], augmented[pivot], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]


def _fit_linear(
    observations: Sequence[V4Observation],
    feature: Callable[[V4Observation], tuple[float, ...]],
    *,
    ridge_lambda: float,
) -> _LinearModel:
    vectors = [feature(item) for item in observations]
    size = len(vectors[0])
    means = tuple(statistics.mean(row[index] for row in vectors) for index in range(size))
    scales = tuple(
        max(statistics.pstdev(row[index] for row in vectors), 1e-9) for index in range(size)
    )
    design = [
        (1.0,)
        + tuple(
            (value - mean) / scale for value, mean, scale in zip(row, means, scales, strict=True)
        )
        for row in vectors
    ]
    targets = [math.log(item.paired_ratio) for item in observations]
    dimension = size + 1
    normal = [[0.0] * dimension for _ in range(dimension)]
    right = [0.0] * dimension
    for row, target in zip(design, targets, strict=True):
        for left in range(dimension):
            right[left] += row[left] * target
            for column in range(dimension):
                normal[left][column] += row[left] * row[column]
    for index in range(1, dimension):
        normal[index][index] += ridge_lambda
    fitted = _solve_linear_system(normal, right)
    return _LinearModel(fitted[0], tuple(fitted[1:]), means, scales)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _support_domain(
    observations: Sequence[V4Observation],
) -> tuple[tuple[int, int], tuple[float, float], tuple[float, float]]:
    return (
        (
            min(item.stats.join_input_rows for item in observations),
            max(item.stats.join_input_rows for item in observations),
        ),
        (
            min(item.stats.sensitive_raw_width_bytes for item in observations),
            max(item.stats.sensitive_raw_width_bytes for item in observations),
        ),
        (
            min(item.stats.join_match_rate for item in observations),
            max(item.stats.join_match_rate for item in observations),
        ),
    )


def _fit_v4(
    training: Sequence[V4Observation],
    protocol_domain: Sequence[V4Observation],
    config: V4ModelDevelopmentConfig,
) -> PipelineV4CostModel:
    def feature(item: V4Observation) -> tuple[float, ...]:
        return pipeline_v4_model_feature_vector(item.stats)

    residuals: list[float] = []
    groups = sorted({item.scenario_group for item in training})
    for held_group in groups:
        inner_train = [item for item in training if item.scenario_group != held_group]
        inner_test = [item for item in training if item.scenario_group == held_group]
        model = _fit_linear(inner_train, feature, ridge_lambda=config.ridge_lambda)
        residuals.extend(
            abs(model.predict(feature(item)) - math.log(item.paired_ratio)) for item in inner_test
        )
    fitted = _fit_linear(training, feature, ridge_lambda=config.ridge_lambda)
    uncertainty = max(
        math.log1p(config.tie_threshold_fraction),
        _nearest_rank(residuals, config.uncertainty_residual_quantile),
    )
    rows, widths, rates = _support_domain(protocol_domain)
    return PipelineV4CostModel(
        intercept_log_ratio=fitted.intercept,
        coefficients=fitted.coefficients,
        feature_means=fitted.means,
        feature_scales=fitted.scales,
        uncertainty_threshold=uncertainty,
        ridge_lambda=config.ridge_lambda,
        training_family_count=len(training),
        training_scenario_groups=tuple(groups),
        support_join_input_rows=rows,
        support_sensitive_width_bytes=widths,
        support_match_rate=rates,
    )


def _load_observations(root: Path, config: V4ModelDevelopmentConfig) -> list[V4Observation]:
    audit = _load_json(root / config.audit_path)
    audited = {
        str(item["family_id"]): item for item in cast(list[dict[str, Any]], audit["family_audits"])
    }
    families = [
        _load_json(path)
        for path in sorted((root / config.calibration_run_dir / "families").glob("*.json"))
    ]
    if set(audited) != {str(item["family_id"]) for item in families}:
        raise ValueError("V4 model input family sets differ")
    return [
        V4Observation(
            family_id=str(item["family_id"]),
            scenario_group=str(item["scenario_group"]),
            stats=RealPipelineWorkloadStats(**cast(dict[str, Any], item["statistics"])),
            paired_ratio=float(audited[str(item["family_id"])]["median_early_over_late_ratio"]),
            stable=bool(audited[str(item["family_id"])]["stable_for_model_evaluation"]),
        )
        for item in families
    ]


def _candidate(placement: MaskPlacement) -> str:
    return EARLY_CANDIDATE if placement == MaskPlacement.EARLY else LATE_CANDIDATE


def _regret(ratio: float, selected: str) -> float:
    if selected == EARLY_CANDIDATE:
        return max(0.0, ratio - 1.0) * 100.0
    return max(0.0, 1.0 / ratio - 1.0) * 100.0


def _metrics(rows: list[dict[str, Any]], scheme: str) -> dict[str, float | int]:
    stable = [item for item in rows if item["stable"]]
    regrets = [float(item[scheme]["regret_percent"]) for item in stable]
    return {
        "evaluated_stable_family_count": len(stable),
        "top1_selection_rate": sum(item[scheme]["top1"] for item in stable) / len(stable),
        "within_3_percent_rate": sum(item <= 3.0 for item in regrets) / len(regrets),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _nearest_rank(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "direct_coverage": sum(item[scheme]["direct"] for item in stable) / len(stable),
        "illegal_selection_count": sum(item[scheme]["illegal"] for item in rows),
    }


def run_v4_grouped_cross_validation(
    root: Path,
    config: V4ModelDevelopmentConfig,
) -> tuple[dict[str, object], PipelineV4CostModel]:
    observations = _load_observations(root, config)
    transfer_config = load_real_optimizer_transfer_config(root / config.v3_transfer_config_path)
    v3_primary, v3_stability = _load_frozen_models(root, transfer_config)
    groups = sorted({item.scenario_group for item in observations})
    predictions: list[dict[str, Any]] = []
    folds: list[dict[str, object]] = []
    for held_group in groups:
        training = [item for item in observations if item.scenario_group != held_group]
        validation = [item for item in observations if item.scenario_group == held_group]
        v4 = _fit_v4(training, observations, config)
        match = _fit_linear(
            training,
            lambda item: (item.stats.join_match_rate,),
            ridge_lambda=config.ridge_lambda,
        )
        folds.append(
            {
                "held_out_group": held_group,
                "training_groups": sorted({item.scenario_group for item in training}),
                "training_family_count": len(training),
                "validation_family_count": len(validation),
                "v4_uncertainty_threshold": v4.uncertainty_threshold,
            }
        )
        for item in validation:
            v4_decision = choose_mask_placement_v4(item.stats, v4)
            match_prediction = match.predict((item.stats.join_match_rate,))
            match_selected = EARLY_CANDIDATE if match_prediction < 0.0 else LATE_CANDIDATE
            features = MaskPlacementFeatures(
                item.stats.join_input_rows,
                int(item.stats.sensitive_raw_width_bytes),
                item.stats.join_match_rate,
            )
            v1_selected = _candidate(choose_mask_placement(features).placement)
            v3_selected = _candidate(
                choose_mask_placement_by_stable_interaction_cost(
                    features, v3_primary, v3_stability
                ).placement
            )
            selections = {
                "fixed_early": (EARLY_CANDIDATE, True),
                "fixed_late": (LATE_CANDIDATE, True),
                "match_rate_baseline": (match_selected, True),
                "optimizer_v1": (v1_selected, True),
                "optimizer_v3": (v3_selected, True),
                "optimizer_v4": (
                    _candidate(v4_decision.placement),
                    v4_decision.direct_model_decision,
                ),
                "oracle": (item.actual_direction, True),
            }
            row: dict[str, Any] = {
                "family_id": item.family_id,
                "held_out_group": held_group,
                "stable": item.stable,
                "paired_ratio": item.paired_ratio,
                "actual_direction": item.actual_direction,
                "v4_reason_code": v4_decision.reason_code,
                "v4_prediction": v4_decision.predicted_log_early_late_ratio,
                "match_rate_prediction": match_prediction,
            }
            for scheme, (selected, direct) in selections.items():
                row[scheme] = {
                    "selected_candidate": selected,
                    "top1": selected == item.actual_direction,
                    "regret_percent": _regret(item.paired_ratio, selected),
                    "direct": direct,
                    "illegal": False,
                }
            predictions.append(row)
    schemes = (
        "fixed_early",
        "fixed_late",
        "match_rate_baseline",
        "optimizer_v1",
        "optimizer_v3",
        "optimizer_v4",
        "oracle",
    )
    metrics = {scheme: _metrics(predictions, scheme) for scheme in schemes}
    unstable = [item for item in predictions if not item["stable"]]
    uncertainty_capture = sum(not item["optimizer_v4"]["direct"] for item in unstable) / len(
        unstable
    )
    v4_metrics = metrics["optimizer_v4"]
    match_metrics = metrics["match_rate_baseline"]
    gates = {
        "minimum_within_3_percent_rate": (
            float(v4_metrics["within_3_percent_rate"]) >= config.gates.minimum_within_3_percent_rate
        ),
        "maximum_mean_regret_percent": (
            float(v4_metrics["mean_regret_percent"]) <= config.gates.maximum_mean_regret_percent
        ),
        "maximum_p95_regret_percent": (
            float(v4_metrics["p95_regret_percent"]) <= config.gates.maximum_p95_regret_percent
        ),
        "maximum_regret_percent": (
            float(v4_metrics["max_regret_percent"]) <= config.gates.maximum_regret_percent
        ),
        "minimum_direct_coverage": (
            float(v4_metrics["direct_coverage"]) >= config.gates.minimum_direct_coverage
        ),
        "minimum_unstable_uncertainty_capture": (
            uncertainty_capture >= config.gates.minimum_unstable_uncertainty_capture
        ),
        "mean_regret_near_match_baseline": (
            float(v4_metrics["mean_regret_percent"])
            <= float(match_metrics["mean_regret_percent"])
            + config.gates.maximum_mean_regret_gap_vs_match_baseline
        ),
        "p95_regret_near_match_baseline": (
            float(v4_metrics["p95_regret_percent"])
            <= float(match_metrics["p95_regret_percent"])
            + config.gates.maximum_p95_regret_gap_vs_match_baseline
        ),
        "no_illegal_selection": int(v4_metrics["illegal_selection_count"]) == 0,
    }
    final_model = _fit_v4(observations, observations, config)
    return (
        {
            "schema_version": 1,
            "status": (
                "PASS_V4_DEVELOPMENT_GATE"
                if all(gates.values())
                else "FAIL_V4_DEVELOPMENT_GATE_RETAIN"
            ),
            "outer_cross_validation": "leave_one_complete_time_window_out",
            "folds": folds,
            "metrics": metrics,
            "unstable_family_count": len(unstable),
            "v4_unstable_uncertainty_capture": uncertainty_capture,
            "gate_checks": gates,
            "predictions": predictions,
            "external_partition_accessed": False,
            "profile_timings_used_as_features": False,
            "support_source": "predeclared_january_protocol_domain_without_labels",
            "scientific_boundary": config.scientific_boundary,
        },
        final_model,
    )


def run_optimizer_v4_model_development(
    config: V4ModelDevelopmentConfig,
    *,
    project_root: Path,
    config_path: Path,
) -> Path:
    root = project_root.resolve()
    if sha256_file(root / config.audit_path) != config.audit_sha256:
        raise ValueError("Frozen V4 calibration audit binding changed")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("V4 model development requires a clean commit")
    result, model = run_v4_grouped_cross_validation(root, config)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / config.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(run_dir / "config.json", asdict(config))
    _atomic_json(
        run_dir / "environment.json",
        {
            "commit_hash": commit,
            "git_dirty": dirty,
            "config_sha256": sha256_file(config_path),
        },
    )
    _atomic_json(run_dir / "cross_validation.json", result)
    _atomic_json(run_dir / "pipeline_v4_model.json", model.to_dict())
    _atomic_json(run_dir.parent / "latest_run.json", {"run_id": run_id})
    return run_dir
