"""Nested, severe-regret-aware development of Optimizer V5.1.

V5.1 preserves the frozen V5 mechanism prior, residual feature contract,
governance checks, uncertainty rule, and acceptance gates.  Its only method
change is to weight complete training families by the magnitude of their
observed early/late separation.  The weight exponent is selected inside each
outer fold by five-fold family-level validation, so the outer family never
influences model selection.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.optimizer_v5_hybrid_development import (
    V5DevelopmentGates,
    summarize_v5_predictions,
)
from trustaero.experiments.pipeline_optimizer import (
    PipelineMaskFamilyObservation,
    load_pipeline_mask_families,
)
from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_data_pilot import _git_state
from trustaero.optimizer.mask import MaskPlacement, choose_mask_placement
from trustaero.optimizer.mask_mechanism import MechanismMaskCostModel
from trustaero.optimizer.mask_pipeline_v5 import (
    PipelineV5HybridModel,
    PipelineV5ResidualSurface,
    choose_mask_placement_v5,
    v5_residual_feature_vector,
    v5_support_feature_vector,
)
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class V51Binding:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class V51NestedConfig:
    protocol_name: str
    results_dir: str
    source_run_dirs: tuple[str, ...]
    immutable_bindings: tuple[V51Binding, ...]
    mechanism_model_path: str
    mechanism_model_sha256: str
    v5_negative_record_path: str
    v5_negative_record_sha256: str
    severity_exponents: tuple[float, ...]
    severity_weight_cap: float
    inner_fold_count: int
    inner_partition_seed: int
    ridge_lambda: float
    uncertainty_multiplier: float
    tie_threshold_fraction: float
    require_clean_git: bool
    gates: V5DevelopmentGates
    scientific_boundary: str

    def __post_init__(self) -> None:
        if (
            len(self.severity_exponents) < 2
            or len(set(self.severity_exponents)) != len(self.severity_exponents)
            or 0.0 not in self.severity_exponents
        ):
            raise ValueError("V5.1 must compare unweighted and weighted objectives")
        if any(value < 0.0 or not math.isfinite(value) for value in self.severity_exponents):
            raise ValueError("V5.1 severity exponents must be nonnegative")
        if self.severity_weight_cap < 1.0 or self.inner_fold_count < 3:
            raise ValueError("V5.1 nested-validation controls are invalid")
        if self.ridge_lambda <= 0.0 or self.uncertainty_multiplier < 0.0:
            raise ValueError("V5.1 fitting controls are invalid")
        if not 0.0 < self.tie_threshold_fraction < 1.0:
            raise ValueError("V5.1 tie threshold must be between zero and one")


def load_v51_nested_config(path: Path | str) -> V51NestedConfig:
    """Load the frozen nested-development protocol."""

    payload = _load_json(Path(path))
    gates = cast(dict[str, Any], payload["gates"])
    return V51NestedConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        source_run_dirs=tuple(str(item) for item in payload["source_run_dirs"]),
        immutable_bindings=tuple(
            V51Binding(path=str(item["path"]), sha256=str(item["sha256"]))
            for item in cast(list[dict[str, Any]], payload["immutable_bindings"])
        ),
        mechanism_model_path=str(payload["mechanism_model_path"]),
        mechanism_model_sha256=str(payload["mechanism_model_sha256"]),
        v5_negative_record_path=str(payload["v5_negative_record_path"]),
        v5_negative_record_sha256=str(payload["v5_negative_record_sha256"]),
        severity_exponents=tuple(float(item) for item in payload["severity_exponents"]),
        severity_weight_cap=float(payload["severity_weight_cap"]),
        inner_fold_count=int(payload["inner_fold_count"]),
        inner_partition_seed=int(payload["inner_partition_seed"]),
        ridge_lambda=float(payload["ridge_lambda"]),
        uncertainty_multiplier=float(payload["uncertainty_multiplier"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
        gates=V5DevelopmentGates(
            minimum_direct_coverage=float(gates["minimum_direct_coverage"]),
            minimum_within_tie_improvement_over_v1=float(
                gates["minimum_within_tie_improvement_over_v1"]
            ),
            maximum_mean_regret_percent=float(gates["maximum_mean_regret_percent"]),
            maximum_p95_regret_percent=float(gates["maximum_p95_regret_percent"]),
            maximum_max_regret_percent=float(gates["maximum_max_regret_percent"]),
        ),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def severity_weight(
    observation: PipelineMaskFamilyObservation,
    exponent: float,
    *,
    cap: float,
) -> float:
    """Upweight clear separations without using a model's current mistakes."""

    if exponent < 0.0 or cap < 1.0:
        raise ValueError("Severity weighting parameters are invalid")
    if exponent == 0.0:
        return 1.0
    tie_log = math.log1p(observation.tie_threshold_fraction)
    normalized = max(1.0, abs(observation.observed_log_early_late_ratio) / tie_log)
    return min(cap, math.pow(normalized, exponent))


def _scalers(vectors: list[tuple[float, ...]]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    columns = list(zip(*vectors, strict=True))
    means = tuple(statistics.mean(column) for column in columns)
    scales = tuple(
        statistics.pstdev(column) if statistics.pstdev(column) > 1e-12 else 1.0
        for column in columns
    )
    return means, scales


def _solve_weighted_ridge(
    vectors: list[tuple[float, ...]],
    targets: list[float],
    weights: list[float],
    *,
    ridge_lambda: float,
) -> tuple[float, tuple[float, ...]]:
    """Solve the tiny weighted ridge system directly and deterministically.

    V5.1 has only six residual features.  Solving its 7-by-7 normal equation is
    substantially faster than repeatedly applying coordinate descent inside
    nested cross-validation.  The intercept is deliberately not penalized.
    """

    if not vectors or len(vectors) != len(targets) or len(vectors) != len(weights):
        raise ValueError("Weighted ridge inputs are invalid")
    parameter_count = len(vectors[0]) + 1
    matrix = [[0.0] * parameter_count for _ in range(parameter_count)]
    right_hand_side = [0.0] * parameter_count
    for vector, target, weight in zip(vectors, targets, weights, strict=True):
        row = (1.0, *vector)
        for left in range(parameter_count):
            right_hand_side[left] += weight * row[left] * target
            for right in range(parameter_count):
                matrix[left][right] += weight * row[left] * row[right]
    for index in range(1, parameter_count):
        matrix[index][index] += ridge_lambda

    # Partial-pivot Gaussian elimination is sufficient for this fixed 7x7
    # positive-definite ridge system and avoids an extra numerical dependency.
    for pivot in range(parameter_count):
        pivot_row = max(
            range(pivot, parameter_count),
            key=lambda row_index: abs(matrix[row_index][pivot]),
        )
        if abs(matrix[pivot_row][pivot]) <= 1e-12:
            raise ValueError("Weighted ridge system is singular")
        matrix[pivot], matrix[pivot_row] = matrix[pivot_row], matrix[pivot]
        right_hand_side[pivot], right_hand_side[pivot_row] = (
            right_hand_side[pivot_row],
            right_hand_side[pivot],
        )
        divisor = matrix[pivot][pivot]
        for column in range(pivot, parameter_count):
            matrix[pivot][column] /= divisor
        right_hand_side[pivot] /= divisor
        for row_index in range(parameter_count):
            if row_index == pivot:
                continue
            factor = matrix[row_index][pivot]
            if abs(factor) <= 1e-18:
                continue
            for column in range(pivot, parameter_count):
                matrix[row_index][column] -= factor * matrix[pivot][column]
            right_hand_side[row_index] -= factor * right_hand_side[pivot]
    return right_hand_side[0], tuple(right_hand_side[1:])


def fit_v51_weighted_surface(
    observations: list[PipelineMaskFamilyObservation],
    mechanism_prior: MechanismMaskCostModel,
    *,
    severity_exponent: float,
    severity_weight_cap: float,
    ridge_lambda: float,
    uncertainty_multiplier: float,
) -> PipelineV5ResidualSurface:
    """Fit the frozen V5 residual basis with family-level severity weights."""

    feature_count = 6
    if len(observations) <= feature_count + 1:
        raise ValueError("V5.1 fitting needs more families than features")
    vectors: list[tuple[float, ...]] = []
    targets: list[float] = []
    weights: list[float] = []
    for item in observations:
        family_weight = severity_weight(item, severity_exponent, cap=severity_weight_cap)
        for placement, latency in (
            (MaskPlacement.EARLY, item.median_early_latency_ms),
            (MaskPlacement.LATE, item.median_late_latency_ms),
        ):
            prior_ms = mechanism_prior.predict_candidate_ms(item.features, placement)
            if prior_ms <= 0.0 or latency <= 0.0:
                raise ValueError("V5.1 costs and targets must be positive")
            vectors.append(
                v5_residual_feature_vector(
                    item.features,
                    placement,
                    hashed_identifier_width_bytes=(mechanism_prior.hashed_identifier_width_bytes),
                )
            )
            targets.append(math.log(latency) - math.log(prior_ms))
            weights.append(family_weight)
    means, scales = _scalers(vectors)
    standardized = [
        tuple(
            (value - mean) / scale for value, mean, scale in zip(vector, means, scales, strict=True)
        )
        for vector in vectors
    ]
    intercept, coefficients = _solve_weighted_ridge(
        standardized,
        targets,
        weights,
        ridge_lambda=ridge_lambda,
    )
    support_vectors = [v5_support_feature_vector(item.features) for item in observations]
    support_columns = list(zip(*support_vectors, strict=True))
    provisional = PipelineV5ResidualSurface(
        intercept_log_ms=intercept,
        coefficients=tuple(coefficients),
        feature_means=means,
        feature_scales=scales,
        support_minima=tuple(min(column) for column in support_columns),
        support_maxima=tuple(max(column) for column in support_columns),
        residual_log_ratio_rmse=0.0,
        uncertainty_multiplier=uncertainty_multiplier,
        ridge_lambda=ridge_lambda,
        training_family_count=len(observations),
        source_run_ids=tuple(sorted({run for item in observations for run in item.source_run_ids})),
    )
    model = PipelineV5HybridModel(mechanism_prior, provisional)
    errors = [
        model.predict_log_early_late_ratio(item.features) - item.observed_log_early_late_ratio
        for item in observations
    ]
    rmse = math.sqrt(statistics.mean(error**2 for error in errors))
    return PipelineV5ResidualSurface(
        intercept_log_ms=provisional.intercept_log_ms,
        coefficients=provisional.coefficients,
        feature_means=provisional.feature_means,
        feature_scales=provisional.feature_scales,
        support_minima=provisional.support_minima,
        support_maxima=provisional.support_maxima,
        residual_log_ratio_rmse=rmse,
        uncertainty_multiplier=uncertainty_multiplier,
        ridge_lambda=ridge_lambda,
        training_family_count=len(observations),
        source_run_ids=provisional.source_run_ids,
    )


def _regret(observed_log_ratio: float, placement: MaskPlacement) -> float:
    ratio = (
        max(1.0, math.exp(observed_log_ratio))
        if placement is MaskPlacement.EARLY
        else max(1.0, math.exp(-observed_log_ratio))
    )
    return (ratio - 1.0) * 100.0


def prediction_rows(
    held_out: PipelineMaskFamilyObservation,
    model: PipelineV5HybridModel,
) -> list[dict[str, object]]:
    """Evaluate one untouched family under V1, direct V5.1, and guarded V5.1."""

    prediction = model.predict_log_early_late_ratio(held_out.features)
    direct = MaskPlacement.EARLY if prediction < 0.0 else MaskPlacement.LATE
    guarded = choose_mask_placement_v5(held_out.features, model)
    v1 = choose_mask_placement(held_out.features).placement
    actual = held_out.observed_log_early_late_ratio
    oracle = MaskPlacement.EARLY if actual < 0.0 else MaskPlacement.LATE
    rows: list[dict[str, object]] = []
    for scheme, placement, direct_credit, reason in (
        ("v1", v1, True, "FROZEN_V1"),
        ("v5_direct", direct, True, "V51_RAW_COMPONENT_RANKING"),
        (
            "v5_guarded",
            guarded.placement,
            guarded.direct_cost_decision,
            guarded.reason_code.replace("MASK_V5_", "MASK_V51_"),
        ),
    ):
        regret = _regret(actual, placement)
        rows.append(
            {
                "family_id": held_out.family_id,
                "scheme": scheme,
                "seed_count": held_out.seed_count,
                "join_input_rows": held_out.features.join_input_rows,
                "identifier_width_bytes": held_out.features.identifier_width_bytes,
                "join_match_rate": held_out.features.join_match_rate,
                "observed_log_early_late_ratio": actual,
                "predicted_log_early_late_ratio": prediction,
                "selected_placement": placement.value,
                "oracle_placement": oracle.value,
                "exact_top1": placement is oracle,
                "within_tie_threshold": regret <= held_out.tie_threshold_fraction * 100.0,
                "regret_percent": regret,
                "direct_cost_decision": direct_credit,
                "reason_code": reason,
                "within_training_support": model.residual_surface.is_within_support(
                    held_out.features
                ),
                "uncertainty_margin": model.residual_surface.uncertainty_margin,
            }
        )
    return rows


def deterministic_inner_folds(
    observations: Sequence[PipelineMaskFamilyObservation],
    *,
    fold_count: int,
    seed: int,
) -> tuple[tuple[PipelineMaskFamilyObservation, ...], ...]:
    """Create balanced, deterministic folds without splitting a family."""

    if fold_count < 2 or len(observations) < fold_count:
        raise ValueError("Inner folds require enough complete families")
    ordered = sorted(
        observations,
        key=lambda item: hashlib.sha256(f"{seed}:{item.family_id}".encode()).hexdigest(),
    )
    folds: list[list[PipelineMaskFamilyObservation]] = [[] for _ in range(fold_count)]
    for index, item in enumerate(ordered):
        folds[index % fold_count].append(item)
    return tuple(tuple(fold) for fold in folds)


def select_severity_exponent(
    observations: list[PipelineMaskFamilyObservation],
    mechanism_prior: MechanismMaskCostModel,
    *,
    severity_exponents: Sequence[float],
    severity_weight_cap: float,
    fold_count: int,
    partition_seed: int,
    ridge_lambda: float,
    uncertainty_multiplier: float,
    gates: V5DevelopmentGates,
) -> tuple[float, list[dict[str, object]]]:
    """Select one exponent entirely inside the caller's training partition."""

    folds = deterministic_inner_folds(observations, fold_count=fold_count, seed=partition_seed)
    candidates: list[dict[str, object]] = []
    for exponent in severity_exponents:
        rows: list[dict[str, object]] = []
        for validation in folds:
            validation_ids = {item.family_id for item in validation}
            training = [item for item in observations if item.family_id not in validation_ids]
            surface = fit_v51_weighted_surface(
                training,
                mechanism_prior,
                severity_exponent=exponent,
                severity_weight_cap=severity_weight_cap,
                ridge_lambda=ridge_lambda,
                uncertainty_multiplier=uncertainty_multiplier,
            )
            model = PipelineV5HybridModel(mechanism_prior, surface)
            for held_out in validation:
                rows.extend(prediction_rows(held_out, model))
        summary = summarize_v5_predictions(rows, gates=gates)
        metrics = cast(dict[str, Any], summary["metrics"])["v5_guarded"]
        coverage_failed = float(metrics["direct_coverage"]) < gates.minimum_direct_coverage
        score = (
            int(coverage_failed),
            float(metrics["max_regret_percent"]),
            float(metrics["p95_regret_percent"]),
            float(metrics["mean_regret_percent"]),
            -float(metrics["within_tie_rate"]),
            -float(metrics["direct_coverage"]),
            float(exponent),
        )
        candidates.append(
            {
                "severity_exponent": float(exponent),
                "inner_metrics": metrics,
                "selection_score": list(score),
            }
        )
    selected = min(candidates, key=lambda item: cast(list[float], item["selection_score"]))
    return float(cast(Any, selected["severity_exponent"])), candidates


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_v51_nested_development(
    config: V51NestedConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume nested outer-family evaluation and full model selection."""

    root = project_root.resolve()
    bindings = (
        *config.immutable_bindings,
        V51Binding(config.mechanism_model_path, config.mechanism_model_sha256),
        V51Binding(config.v5_negative_record_path, config.v5_negative_record_sha256),
    )
    for binding in bindings:
        if sha256_file(root / binding.path) != binding.sha256:
            raise ValueError(f"V5.1 development input changed: {binding.path}")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("V5.1 development requires a clean commit")
    observations = load_pipeline_mask_families(
        [root / item for item in config.source_run_dirs],
        tie_threshold_fraction=config.tie_threshold_fraction,
    )
    prior = MechanismMaskCostModel.from_dict(_load_json(root / config.mechanism_model_path))
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / config.results_dir / run_id
    folds_dir = run_dir / "outer_folds"
    folds_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(json.dumps(asdict(config), sort_keys=True))
    config_path = run_dir / "config.json"
    if resume_run_id and config_path.is_file() and _load_json(config_path) != payload:
        raise ValueError("V5.1 resume config changed")
    _atomic_json(config_path, payload)
    _atomic_json(
        run_dir / "environment.json",
        {"commit_hash": commit, "git_dirty": dirty, "gpu_acceleration": False},
    )
    if resume_run_id is None:
        _atomic_json(run_dir.parent / "latest_run.json", {"run_id": run_id})
    started = time.perf_counter()
    total = len(observations)
    for index, held_out in enumerate(observations, start=1):
        fold_path = folds_dir / f"{held_out.family_id}.json"
        if not fold_path.is_file():
            outer_training = [item for item in observations if item.family_id != held_out.family_id]
            exponent, inner = select_severity_exponent(
                outer_training,
                prior,
                severity_exponents=config.severity_exponents,
                severity_weight_cap=config.severity_weight_cap,
                fold_count=config.inner_fold_count,
                partition_seed=config.inner_partition_seed,
                ridge_lambda=config.ridge_lambda,
                uncertainty_multiplier=config.uncertainty_multiplier,
                gates=config.gates,
            )
            surface = fit_v51_weighted_surface(
                outer_training,
                prior,
                severity_exponent=exponent,
                severity_weight_cap=config.severity_weight_cap,
                ridge_lambda=config.ridge_lambda,
                uncertainty_multiplier=config.uncertainty_multiplier,
            )
            model = PipelineV5HybridModel(prior, surface)
            _atomic_json(
                fold_path,
                {
                    "outer_holdout_family_id": held_out.family_id,
                    "selected_severity_exponent": exponent,
                    "inner_candidates": inner,
                    "rows": prediction_rows(held_out, model),
                },
            )
        if progress_callback is not None:
            progress_callback(index, total, held_out.family_id, time.perf_counter() - started)
    fold_payloads = [_load_json(path) for path in sorted(folds_dir.glob("*.json"))]
    if len(fold_payloads) != total:
        raise ValueError("V5.1 outer cross-validation is incomplete")
    rows = [
        row
        for payload_row in fold_payloads
        for row in cast(list[dict[str, object]], payload_row["rows"])
    ]
    summary = summarize_v5_predictions(rows, gates=config.gates)
    freeze_authorized = bool(summary.pop("v5_model_freeze_authorized"))
    selected_full, full_inner = select_severity_exponent(
        observations,
        prior,
        severity_exponents=config.severity_exponents,
        severity_weight_cap=config.severity_weight_cap,
        fold_count=config.inner_fold_count,
        partition_seed=config.inner_partition_seed,
        ridge_lambda=config.ridge_lambda,
        uncertainty_multiplier=config.uncertainty_multiplier,
        gates=config.gates,
    )
    full_surface = fit_v51_weighted_surface(
        observations,
        prior,
        severity_exponent=selected_full,
        severity_weight_cap=config.severity_weight_cap,
        ridge_lambda=config.ridge_lambda,
        uncertainty_multiplier=config.uncertainty_multiplier,
    )
    model = PipelineV5HybridModel(prior, full_surface)
    selections = {
        "outer_selected_exponents": [
            {
                "family_id": item["outer_holdout_family_id"],
                "severity_exponent": item["selected_severity_exponent"],
            }
            for item in fold_payloads
        ],
        "full_selected_severity_exponent": selected_full,
        "full_inner_candidates": full_inner,
        "selection_objective": [
            "coverage_failure",
            "maximum_regret",
            "p95_regret",
            "mean_regret",
            "negative_within_tie_rate",
            "negative_direct_coverage",
            "smaller_exponent_tie_break",
        ],
    }
    summary.update(
        {
            "status": (
                "PASS_OPTIMIZER_V51_NESTED_DEVELOPMENT_GATE"
                if freeze_authorized
                else "FAIL_OPTIMIZER_V51_NESTED_DEVELOPMENT_GATE_RETAIN"
            ),
            "v51_model_freeze_authorized": freeze_authorized,
            "nested_validation": True,
            "inner_fold_count": config.inner_fold_count,
            "full_selected_severity_exponent": selected_full,
            "scientific_boundary": config.scientific_boundary,
        }
    )
    _write_csv(run_dir / "outer_cross_validation.csv", rows)
    _atomic_json(run_dir / "hyperparameter_selections.json", selections)
    _atomic_json(run_dir / "summary.json", summary)
    _atomic_json(run_dir / "pipeline_v51_hybrid_model.json", model.to_dict())
    return run_dir
