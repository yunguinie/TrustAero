"""Nested complete-family development evaluation for Optimizer V3."""

from __future__ import annotations

import csv
import json
import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.pipeline_optimizer import (
    PipelineMaskFamilyObservation,
    load_pipeline_mask_families,
)
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures, choose_mask_placement
from trustaero.optimizer.mask_interaction import (
    INTERACTION_FEATURE_NAMES,
    InteractionMaskCostModel,
    choose_mask_placement_by_stable_interaction_cost,
    interaction_feature_vector,
    interaction_support_vector,
)
from trustaero.reproducibility.source_freeze import audit_source_freeze


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        fields.extend(field for field in row if field not in fields)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Return the existing experiment suite's nearest-rank percentile."""

    if not values:
        raise ValueError("A percentile requires at least one value")
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _linear_quantile(values: Sequence[float], fraction: float) -> float:
    """Interpolate an uncertainty residual quantile deterministically."""

    if not values:
        raise ValueError("A residual quantile requires at least one value")
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _solve_linear_system(matrix: list[list[float]], target: list[float]) -> list[float]:
    """Solve a small dense system with partial pivoting and no extra dependency."""

    size = len(target)
    augmented = [row[:] + [value] for row, value in zip(matrix, target, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-12:
            raise ValueError("Interaction ridge system is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[row][-1] for row in range(size)]


def fit_interaction_mask_cost_model(
    observations: Sequence[PipelineMaskFamilyObservation],
    *,
    ridge_lambda: float,
    uncertainty_residual_quantile: float = 0.0,
    uncertainty_threshold: float = 0.0,
) -> InteractionMaskCostModel:
    """Fit the frozen ridge basis to complete-family paired log ratios."""

    if len(observations) <= len(INTERACTION_FEATURE_NAMES) + 1:
        raise ValueError("Interaction fitting lacks independent workload families")
    if ridge_lambda <= 0.0:
        raise ValueError("ridge_lambda must be positive")
    vectors = [interaction_feature_vector(item.features) for item in observations]
    targets = [item.observed_log_early_late_ratio for item in observations]
    columns = list(zip(*vectors, strict=True))
    means = tuple(statistics.mean(column) for column in columns)
    scales = tuple(
        statistics.pstdev(column) if statistics.pstdev(column) > 1e-12 else 1.0
        for column in columns
    )
    standardized = [
        (1.0,)
        + tuple(
            (value - mean) / scale for value, mean, scale in zip(vector, means, scales, strict=True)
        )
        for vector in vectors
    ]
    dimension = len(INTERACTION_FEATURE_NAMES) + 1
    normal = [[0.0 for _ in range(dimension)] for _ in range(dimension)]
    right = [0.0 for _ in range(dimension)]
    for vector, target in zip(standardized, targets, strict=True):
        for row in range(dimension):
            right[row] += vector[row] * target
            for column in range(dimension):
                normal[row][column] += vector[row] * vector[column]
    for index in range(1, dimension):
        normal[index][index] += ridge_lambda
    fitted = _solve_linear_system(normal, right)
    support_vectors = [interaction_support_vector(item.features) for item in observations]
    support_columns = list(zip(*support_vectors, strict=True))
    return InteractionMaskCostModel(
        intercept_log_ratio=fitted[0],
        coefficients=tuple(fitted[1:]),
        feature_means=means,
        feature_scales=scales,
        support_minima=tuple(min(column) for column in support_columns),
        support_maxima=tuple(max(column) for column in support_columns),
        ridge_lambda=ridge_lambda,
        uncertainty_residual_quantile=uncertainty_residual_quantile,
        uncertainty_threshold=uncertainty_threshold,
        training_family_count=len(observations),
        source_run_ids=tuple(
            sorted({run_id for item in observations for run_id in item.source_run_ids})
        ),
    )


def _regret(item: PipelineMaskFamilyObservation, placement: MaskPlacement) -> float:
    actual = item.observed_log_early_late_ratio
    ratio = (
        max(1.0, math.exp(actual))
        if placement is MaskPlacement.EARLY
        else max(1.0, math.exp(-actual))
    )
    return (ratio - 1.0) * 100.0


def _prediction_row(
    item: PipelineMaskFamilyObservation,
    *,
    scheme: str,
    placement: MaskPlacement,
    predicted_log_ratio: float | None,
    used_fallback: bool,
    direct_model_decision: bool,
    reason_code: str,
    ridge_lambda: float | None = None,
    uncertainty_quantile: float | None = None,
    uncertainty_threshold: float | None = None,
    stability_guard_passed: bool | None = None,
    stability_agreement_fraction: float | None = None,
) -> dict[str, Any]:
    actual = item.observed_log_early_late_ratio
    oracle = MaskPlacement.EARLY if actual < 0.0 else MaskPlacement.LATE
    regret = _regret(item, placement)
    return {
        "evaluation_scheme": scheme,
        "holdout_family_id": item.family_id,
        "family_id": item.family_id,
        "seed_count": item.seed_count,
        "join_input_rows": item.features.join_input_rows,
        "identifier_width_bytes": item.features.identifier_width_bytes,
        "join_match_rate": item.features.join_match_rate,
        "observed_log_early_late_ratio": actual,
        "predicted_log_early_late_ratio": predicted_log_ratio,
        "selected_placement": placement.value,
        "oracle_placement": oracle.value,
        "exact_top1": placement is oracle,
        "within_tie_threshold": regret <= item.tie_threshold_fraction * 100.0,
        "regret_percent": regret,
        "speedup_vs_fixed_late_ratio": (
            math.exp(-actual) if placement is MaskPlacement.EARLY else 1.0
        ),
        "speedup_vs_fixed_early_ratio": (
            1.0 if placement is MaskPlacement.EARLY else math.exp(actual)
        ),
        "used_fallback": used_fallback,
        "direct_model_decision": direct_model_decision,
        "reason_code": reason_code,
        "selected_ridge_lambda": ridge_lambda,
        "selected_uncertainty_quantile": uncertainty_quantile,
        "uncertainty_threshold": uncertainty_threshold,
        "stability_guard_passed": stability_guard_passed,
        "stability_agreement_fraction": stability_agreement_fraction,
    }


def _scheme_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    regrets = [float(row["regret_percent"]) for row in rows]
    direct = [row for row in rows if bool(row["direct_model_decision"])]
    return {
        "family_count": len(rows),
        "exact_top1_rate": sum(bool(row["exact_top1"]) for row in rows) / len(rows),
        "within_tie_rate": sum(bool(row["within_tie_threshold"]) for row in rows) / len(rows),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _percentile(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "geometric_speedup_vs_fixed_late_ratio": math.exp(
            statistics.mean(math.log(float(row["speedup_vs_fixed_late_ratio"])) for row in rows)
        ),
        "geometric_speedup_vs_fixed_early_ratio": math.exp(
            statistics.mean(math.log(float(row["speedup_vs_fixed_early_ratio"])) for row in rows)
        ),
        "direct_model_decision_count": len(direct),
        "direct_model_coverage": len(direct) / len(rows),
        "direct_early_decision_count": sum(
            row["selected_placement"] == MaskPlacement.EARLY.value for row in direct
        ),
        "direct_late_decision_count": sum(
            row["selected_placement"] == MaskPlacement.LATE.value for row in direct
        ),
    }


def _fold_predictions(
    observations: Sequence[PipelineMaskFamilyObservation], ridge_lambda: float
) -> list[tuple[PipelineMaskFamilyObservation, InteractionMaskCostModel, float]]:
    output: list[tuple[PipelineMaskFamilyObservation, InteractionMaskCostModel, float]] = []
    for held_out in observations:
        training = [item for item in observations if item.family_id != held_out.family_id]
        model = fit_interaction_mask_cost_model(training, ridge_lambda=ridge_lambda)
        output.append((held_out, model, model.predict_log_early_late_ratio(held_out.features)))
    return output


def _stability_summary(
    models: Sequence[InteractionMaskCostModel], features: MaskPlacementFeatures
) -> tuple[bool, float]:
    placements = [
        MaskPlacement.EARLY
        if model.predict_log_early_late_ratio(features) < 0.0
        else MaskPlacement.LATE
        for model in models
    ]
    largest_count = max(placements.count(candidate) for candidate in MaskPlacement)
    return len(set(placements)) == 1, largest_count / len(placements)


def _inner_candidate_rows(
    folds_by_ridge: dict[
        float,
        list[tuple[PipelineMaskFamilyObservation, InteractionMaskCostModel, float]],
    ],
    *,
    ridge_lambda: float,
    uncertainty_quantile: float,
) -> tuple[list[dict[str, Any]], float]:
    folds = folds_by_ridge[ridge_lambda]
    residuals = [
        abs(prediction - item.observed_log_early_late_ratio) for item, _model, prediction in folds
    ]
    threshold = _linear_quantile(residuals, uncertainty_quantile)
    rows: list[dict[str, Any]] = []
    for index, (item, model, prediction) in enumerate(folds):
        calibrated = replace(
            model,
            uncertainty_residual_quantile=uncertainty_quantile,
            uncertainty_threshold=threshold,
        )
        stability_models = tuple(
            folds_by_ridge[ridge][index][1] for ridge in sorted(folds_by_ridge)
        )
        stability_passed, stability_agreement = _stability_summary(stability_models, item.features)
        decision = choose_mask_placement_by_stable_interaction_cost(
            item.features,
            calibrated,
            stability_models,
        )
        rows.append(
            _prediction_row(
                item,
                scheme="inner_model_selection",
                placement=decision.placement,
                predicted_log_ratio=prediction,
                used_fallback=decision.used_fallback,
                direct_model_decision=decision.direct_model_decision,
                reason_code=decision.reason_code,
                ridge_lambda=ridge_lambda,
                uncertainty_quantile=uncertainty_quantile,
                uncertainty_threshold=threshold,
                stability_guard_passed=stability_passed,
                stability_agreement_fraction=stability_agreement,
            )
        )
    return rows, threshold


def _selection_key(
    summary: dict[str, Any], *, ridge_lambda: float, uncertainty_quantile: float
) -> tuple[float, ...]:
    """Implement the protocol's frozen lexicographic objective."""

    return (
        -float(summary["within_tie_rate"]),
        float(summary["p95_regret_percent"]),
        float(summary["max_regret_percent"]),
        float(summary["mean_regret_percent"]),
        -float(summary["direct_model_coverage"]),
        -uncertainty_quantile,
        -ridge_lambda,
    )


def select_inner_hyperparameters(
    observations: Sequence[PipelineMaskFamilyObservation],
    *,
    ridge_grid: Sequence[float],
    uncertainty_quantile_grid: Sequence[float],
) -> tuple[float, float, float, list[dict[str, Any]]]:
    """Select parameters without observing the outer held-out family."""

    candidates: list[dict[str, Any]] = []
    thresholds: dict[tuple[float, float], float] = {}
    folds_by_ridge = {
        ridge_lambda: _fold_predictions(observations, ridge_lambda) for ridge_lambda in ridge_grid
    }
    for ridge_lambda in ridge_grid:
        for quantile in uncertainty_quantile_grid:
            rows, threshold = _inner_candidate_rows(
                folds_by_ridge,
                ridge_lambda=ridge_lambda,
                uncertainty_quantile=quantile,
            )
            summary = _scheme_summary(rows)
            candidates.append(
                {
                    "ridge_lambda": ridge_lambda,
                    "uncertainty_quantile": quantile,
                    "uncertainty_threshold": threshold,
                    **summary,
                }
            )
            thresholds[(ridge_lambda, quantile)] = threshold
    selected = min(
        candidates,
        key=lambda row: _selection_key(
            row,
            ridge_lambda=float(row["ridge_lambda"]),
            uncertainty_quantile=float(row["uncertainty_quantile"]),
        ),
    )
    ridge = float(selected["ridge_lambda"])
    quantile = float(selected["uncertainty_quantile"])
    return ridge, quantile, thresholds[(ridge, quantile)], candidates


def cross_validate_interaction_optimizer(
    observations: Sequence[PipelineMaskFamilyObservation],
    *,
    ridge_grid: Sequence[float],
    uncertainty_quantile_grid: Sequence[float],
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run nested complete-family cross-validation."""

    predictions: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    started = time.perf_counter()
    total = len(observations)
    for index, held_out in enumerate(observations, start=1):
        training = [item for item in observations if item.family_id != held_out.family_id]
        ridge, quantile, threshold, inner_candidates = select_inner_hyperparameters(
            training,
            ridge_grid=ridge_grid,
            uncertainty_quantile_grid=uncertainty_quantile_grid,
        )
        model = fit_interaction_mask_cost_model(
            training,
            ridge_lambda=ridge,
            uncertainty_residual_quantile=quantile,
            uncertainty_threshold=threshold,
        )
        stability_models = tuple(
            fit_interaction_mask_cost_model(training, ridge_lambda=value) for value in ridge_grid
        )
        stability_passed, stability_agreement = _stability_summary(
            stability_models, held_out.features
        )
        decision = choose_mask_placement_by_stable_interaction_cost(
            held_out.features,
            model,
            stability_models,
        )
        predictions.append(
            _prediction_row(
                held_out,
                scheme="v3_nested_complete_family",
                placement=decision.placement,
                predicted_log_ratio=decision.predicted_log_early_late_ratio,
                used_fallback=decision.used_fallback,
                direct_model_decision=decision.direct_model_decision,
                reason_code=decision.reason_code,
                ridge_lambda=ridge,
                uncertainty_quantile=quantile,
                uncertainty_threshold=threshold,
                stability_guard_passed=stability_passed,
                stability_agreement_fraction=stability_agreement,
            )
        )
        selected_summary = next(
            row
            for row in inner_candidates
            if float(row["ridge_lambda"]) == ridge
            and float(row["uncertainty_quantile"]) == quantile
        )
        selections.append(
            {
                "outer_holdout_family_id": held_out.family_id,
                "selected_ridge_lambda": ridge,
                "selected_uncertainty_quantile": quantile,
                "selected_uncertainty_threshold": threshold,
                "inner_within_tie_rate": selected_summary["within_tie_rate"],
                "inner_mean_regret_percent": selected_summary["mean_regret_percent"],
                "inner_p95_regret_percent": selected_summary["p95_regret_percent"],
                "inner_max_regret_percent": selected_summary["max_regret_percent"],
                "inner_direct_model_coverage": selected_summary["direct_model_coverage"],
            }
        )
        if progress_callback is not None:
            progress_callback(index, total, held_out.family_id, time.perf_counter() - started)
    return predictions, selections


def _baseline_rows(
    observations: Sequence[PipelineMaskFamilyObservation],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in observations:
        v1 = choose_mask_placement(item.features)
        oracle = (
            MaskPlacement.EARLY if item.observed_log_early_late_ratio < 0.0 else MaskPlacement.LATE
        )
        for scheme, placement in (
            ("fixed_early", MaskPlacement.EARLY),
            ("fixed_late", MaskPlacement.LATE),
            ("v1_frozen_baseline", v1.placement),
            ("oracle_experimental_upper_bound", oracle),
        ):
            output.append(
                _prediction_row(
                    item,
                    scheme=scheme,
                    placement=placement,
                    predicted_log_ratio=(
                        item.observed_log_early_late_ratio
                        if scheme == "oracle_experimental_upper_bound"
                        else None
                    ),
                    used_fallback=False,
                    direct_model_decision=False,
                    reason_code=v1.reason_code if scheme == "v1_frozen_baseline" else "",
                )
            )
    return output


def _governance_audit(
    model: InteractionMaskCostModel,
    stability_models: tuple[InteractionMaskCostModel, ...],
) -> dict[str, bool]:
    exposure = choose_mask_placement_by_stable_interaction_cost(
        MaskPlacementFeatures(100_000, 256, 1.0, max_raw_exposure_rows=0),
        model,
        stability_models,
    )
    early_illegal = choose_mask_placement_by_stable_interaction_cost(
        MaskPlacementFeatures(100_000, 256, 1.0, early_mask_legal=False),
        model,
        stability_models,
    )
    fail_closed = False
    try:
        choose_mask_placement_by_stable_interaction_cost(
            MaskPlacementFeatures(
                100_000,
                256,
                1.0,
                early_mask_legal=False,
                late_mask_legal=False,
            ),
            model,
            stability_models,
        )
    except ValueError:
        fail_closed = True
    return {
        "raw_exposure_limit_forces_early": exposure.placement is MaskPlacement.EARLY,
        "early_illegal_forces_late": early_illegal.placement is MaskPlacement.LATE,
        "no_legal_candidate_fails_closed": fail_closed,
        "governance_forced_decisions_are_not_model_decisions": (
            not exposure.direct_model_decision and not early_illegal.direct_model_decision
        ),
    }


def _observation_rows(
    observations: Sequence[PipelineMaskFamilyObservation],
) -> list[dict[str, Any]]:
    return [
        {
            "family_id": item.family_id,
            "source_run_ids": "|".join(item.source_run_ids),
            "source_commit_hashes": "|".join(item.source_commit_hashes),
            "seed_count": item.seed_count,
            "join_input_rows": item.features.join_input_rows,
            "identifier_width_bytes": item.features.identifier_width_bytes,
            "join_match_rate": item.features.join_match_rate,
            "median_early_latency_ms": item.median_early_latency_ms,
            "median_late_latency_ms": item.median_late_latency_ms,
            "observed_log_early_late_ratio": item.observed_log_early_late_ratio,
            "tie_threshold_fraction": item.tie_threshold_fraction,
        }
        for item in observations
    ]


def _report(summary: dict[str, Any]) -> str:
    schemes = cast(dict[str, dict[str, Any]], summary["schemes"])
    lines = [
        "# Optimizer V3 bounded-interaction development",
        "",
        "This is nested complete-family development evaluation, not Phase 2G.",
        "",
        "| Scheme | Top-1 | Within 3% | Mean regret | P95 | Max | Direct coverage |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in schemes.items():
        lines.append(
            "| {name} | {top:.1%} | {within:.1%} | {mean:.2f}% | {p95:.2f}% | "
            "{maximum:.2f}% | {coverage:.1%} |".format(
                name=name,
                top=values["exact_top1_rate"],
                within=values["within_tie_rate"],
                mean=values["mean_regret_percent"],
                p95=values["p95_regret_percent"],
                maximum=values["max_regret_percent"],
                coverage=values["direct_model_coverage"],
            )
        )
    lines.extend(
        [
            "",
            f"Development gate passed: **{summary['development_gate']['passes']}**",
            "",
            "> Phase 2I/J are development data. Passing does not authorize Phase 2G or a ",
            "> paper claim; the model and protocol require a separate immutable freeze.",
        ]
    )
    return "\n".join(lines) + "\n"


def develop_interaction_optimizer(
    project_root: Path,
    config_path: Path,
    *,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Execute the single frozen V3-v3 development protocol."""

    config = _read_object(config_path)
    readiness = _read_object(project_root / str(config["readiness_audit_path"]))
    source_audit = audit_source_freeze(project_root, expected_environment="TrustAero_env")
    if source_audit.status != "READY":
        raise ValueError("Optimizer V3 development requires a clean frozen source snapshot")
    if (
        readiness.get("status") != config["required_readiness_status"]
        or readiness.get("optimizer_v3_protocol_design_authorized") is not True
        or readiness.get("phase2g_authorized") is not False
        or readiness.get("source_commit") != source_audit.source_commit
    ):
        raise ValueError("Optimizer V3 readiness audit is stale or does not authorize development")
    if config.get("phase2g_authorized") is not False:
        raise ValueError("Development configuration must keep Phase 2G unauthorized")
    if config.get("fixed_feature_basis") != list(INTERACTION_FEATURE_NAMES):
        raise ValueError("Configured V3 feature basis differs from the frozen implementation")
    stability_guard = cast(dict[str, Any], config.get("stability_guard", {}))
    if (
        stability_guard.get("type") != "unanimous_ridge_sign_consensus"
        or float(stability_guard.get("required_agreement_fraction", 0.0)) != 1.0
    ):
        raise ValueError("Configured V3 stability guard differs from the frozen implementation")

    run_dirs = [project_root / str(path) for path in cast(list[str], config["source_run_dirs"])]
    observations = load_pipeline_mask_families(
        cast(list[str | Path], run_dirs),
        tie_threshold_fraction=float(config["tie_threshold_fraction"]),
    )
    ridge_grid = tuple(float(value) for value in cast(list[float], config["ridge_lambda_grid"]))
    quantile_grid = tuple(
        float(value) for value in cast(list[float], config["uncertainty_quantile_grid"])
    )
    v3_rows, selections = cross_validate_interaction_optimizer(
        observations,
        ridge_grid=ridge_grid,
        uncertainty_quantile_grid=quantile_grid,
        progress_callback=progress_callback,
    )
    predictions = _baseline_rows(observations) + v3_rows

    full_ridge, full_quantile, full_threshold, full_candidates = select_inner_hyperparameters(
        observations,
        ridge_grid=ridge_grid,
        uncertainty_quantile_grid=quantile_grid,
    )
    final_model = fit_interaction_mask_cost_model(
        observations,
        ridge_lambda=full_ridge,
        uncertainty_residual_quantile=full_quantile,
        uncertainty_threshold=full_threshold,
    )
    final_stability_models = tuple(
        fit_interaction_mask_cost_model(observations, ridge_lambda=value) for value in ridge_grid
    )
    by_scheme: dict[str, list[dict[str, Any]]] = {}
    for row in predictions:
        by_scheme.setdefault(str(row["evaluation_scheme"]), []).append(row)
    schemes = {name: _scheme_summary(rows) for name, rows in by_scheme.items()}
    primary = schemes["v3_nested_complete_family"]
    baseline = cast(dict[str, Any], config["baseline"])
    governance = _governance_audit(final_model, final_stability_models)
    gate_checks = {
        "within_tie_rate_strictly_improves_v1": (
            primary["within_tie_rate"] > float(baseline["within_tie_rate"])
        ),
        "mean_regret_does_not_worsen_v1": (
            primary["mean_regret_percent"] <= float(baseline["mean_regret_percent"]) + 1e-9
        ),
        "p95_regret_does_not_worsen_v1": (
            primary["p95_regret_percent"] <= float(baseline["p95_regret_percent"]) + 1e-9
        ),
        "max_regret_does_not_worsen_v1": (
            primary["max_regret_percent"] <= float(baseline["max_regret_percent"]) + 1e-9
        ),
        "direct_model_coverage_meets_minimum": (
            primary["direct_model_coverage"] >= float(config["minimum_direct_coverage"])
        ),
        "direct_decisions_include_early_and_late": (
            primary["direct_early_decision_count"] > 0 and primary["direct_late_decision_count"] > 0
        ),
        "all_governance_audits_pass": all(governance.values()),
        "feature_basis_matches_protocol": (
            config["fixed_feature_basis"] == list(INTERACTION_FEATURE_NAMES)
        ),
        "stability_guard_matches_protocol": (
            stability_guard.get("type") == "unanimous_ridge_sign_consensus"
            and float(stability_guard.get("required_agreement_fraction", 0.0)) == 1.0
            and len(final_stability_models) == len(ridge_grid)
        ),
    }
    gate_passes = all(gate_checks.values())
    output_dir = project_root / str(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "evaluation_label": config["protocol_name"],
        "evaluation_scope": config["evaluation_scope"],
        "status": (
            "eligible_for_separate_model_freeze"
            if gate_passes
            else "development_only_rejected_by_predeclared_gate"
        ),
        "phase2g_authorized": False,
        "family_count": len(observations),
        "replicate_count": sum(item.seed_count for item in observations),
        "feature_names": list(INTERACTION_FEATURE_NAMES),
        "outer_cross_validation": config["outer_cross_validation"],
        "inner_model_selection": config["inner_model_selection"],
        "selected_full_model": {
            "ridge_lambda": full_ridge,
            "uncertainty_residual_quantile": full_quantile,
            "uncertainty_threshold": full_threshold,
            "stability_ridge_lambdas": list(ridge_grid),
        },
        "schemes": schemes,
        "governance_legality_audit": governance,
        "development_gate": {"passes": gate_passes, "checks": gate_checks},
        "scientific_boundary": (
            "Phase 2I/J informed V3 and remain development data. Nested family CV may "
            "authorize a model freeze only; Phase 2G remains untouched and unauthorized."
        ),
    }
    _write_csv(output_dir / "family_observations.csv", _observation_rows(observations))
    _write_csv(output_dir / "outer_predictions.csv", predictions)
    _write_csv(output_dir / "outer_hyperparameter_selections.csv", selections)
    _write_csv(output_dir / "full_inner_candidates.csv", full_candidates)
    _write_json(output_dir / "interaction_mask_cost_model.json", final_model.to_dict())
    _write_json(
        output_dir / "interaction_stability_models.json",
        {
            "model_type": "unanimous_ridge_sign_consensus",
            "required_agreement_fraction": 1.0,
            "models": [model.to_dict() for model in final_stability_models],
        },
    )
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(_report(summary), encoding="utf-8")
    return output_dir
