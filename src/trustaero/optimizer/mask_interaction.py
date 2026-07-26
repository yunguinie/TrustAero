"""Bounded interaction model for two already-legal Mask placements.

The feature basis is deliberately fixed and continuous.  It can express that
rows, sensitive-field width, and Join match rate jointly change the preferred
pipeline without learning family IDs or hand-written grid thresholds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.mask import (
    MaskPlacement,
    MaskPlacementFeatures,
    choose_mask_placement,
)

INTERACTION_FEATURE_NAMES = (
    "rows_log",
    "width_log",
    "match_rate",
    "rows_log_x_width_log",
    "rows_log_x_match_rate",
    "width_log_x_match_rate",
    "rows_log_x_width_log_x_match_rate",
    "rows_log_squared",
    "width_log_squared",
    "match_rate_squared",
)
INTERACTION_SUPPORT_NAMES = (
    "log1p_join_input_rows_per_100k",
    "log1p_identifier_width_per_64_bytes",
    "join_match_rate",
)


def interaction_support_vector(features: MaskPlacementFeatures) -> tuple[float, ...]:
    """Return the three pre-execution workload dimensions."""

    return (
        math.log1p(features.join_input_rows / 100_000.0),
        math.log1p(features.identifier_width_bytes / 64.0),
        features.join_match_rate,
    )


def interaction_feature_vector(features: MaskPlacementFeatures) -> tuple[float, ...]:
    """Expand the fixed basis without learned thresholds or runtime labels."""

    rows_log, width_log, match_rate = interaction_support_vector(features)
    return (
        rows_log,
        width_log,
        match_rate,
        rows_log * width_log,
        rows_log * match_rate,
        width_log * match_rate,
        rows_log * width_log * match_rate,
        rows_log**2,
        width_log**2,
        match_rate**2,
    )


@dataclass(frozen=True, slots=True)
class InteractionMaskCostModel:
    """Serializable paired log-cost surface and uncertainty boundary."""

    intercept_log_ratio: float
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    support_minima: tuple[float, ...]
    support_maxima: tuple[float, ...]
    ridge_lambda: float
    uncertainty_residual_quantile: float
    uncertainty_threshold: float
    training_family_count: int
    source_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        feature_count = len(INTERACTION_FEATURE_NAMES)
        if not (
            len(self.coefficients)
            == len(self.feature_means)
            == len(self.feature_scales)
            == feature_count
        ):
            raise ValueError(f"Interaction model requires {feature_count} features")
        support_count = len(INTERACTION_SUPPORT_NAMES)
        if not (len(self.support_minima) == len(self.support_maxima) == support_count):
            raise ValueError(f"Interaction model requires {support_count} support dimensions")
        if any(scale <= 0.0 for scale in self.feature_scales):
            raise ValueError("Interaction feature scales must be positive")
        if any(
            lower > upper
            for lower, upper in zip(self.support_minima, self.support_maxima, strict=True)
        ):
            raise ValueError("Interaction support bounds are invalid")
        if self.ridge_lambda <= 0.0:
            raise ValueError("Interaction ridge_lambda must be positive")
        if not 0.0 <= self.uncertainty_residual_quantile <= 1.0:
            raise ValueError("Interaction residual quantile must be in [0, 1]")
        if self.uncertainty_threshold < 0.0:
            raise ValueError("Interaction uncertainty threshold must be non-negative")
        if self.training_family_count <= 0 or not self.source_run_ids:
            raise ValueError("Interaction model must retain training provenance")

    def predict_log_early_late_ratio(self, features: MaskPlacementFeatures) -> float:
        """Predict log(early latency / late latency)."""

        vector = interaction_feature_vector(features)
        standardized = (
            (value - mean) / scale
            for value, mean, scale in zip(
                vector, self.feature_means, self.feature_scales, strict=True
            )
        )
        return self.intercept_log_ratio + sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, standardized, strict=True)
        )

    def is_within_training_support(self, features: MaskPlacementFeatures) -> bool:
        values = interaction_support_vector(features)
        return all(
            lower <= value <= upper
            for value, lower, upper in zip(
                values, self.support_minima, self.support_maxima, strict=True
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "bounded_interaction_pairwise_ridge",
            "model_schema_version": 1,
            "target": "paired_log_early_late_latency_ratio",
            "feature_names": list(INTERACTION_FEATURE_NAMES),
            "support_feature_names": list(INTERACTION_SUPPORT_NAMES),
            "intercept_log_ratio": self.intercept_log_ratio,
            "coefficients": list(self.coefficients),
            "feature_means": list(self.feature_means),
            "feature_scales": list(self.feature_scales),
            "support_minima": list(self.support_minima),
            "support_maxima": list(self.support_maxima),
            "ridge_lambda": self.ridge_lambda,
            "uncertainty_residual_quantile": self.uncertainty_residual_quantile,
            "uncertainty_threshold": self.uncertainty_threshold,
            "training_family_count": self.training_family_count,
            "source_run_ids": list(self.source_run_ids),
            "scientific_boundary": (
                "Candidate legality and exposure limits precede ranking; uncertain "
                "or out-of-support predictions fall back to frozen V1."
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InteractionMaskCostModel:
        if payload.get("model_type") != "bounded_interaction_pairwise_ridge":
            raise ValueError("Unsupported interaction Mask model_type")
        if payload.get("feature_names") != list(INTERACTION_FEATURE_NAMES):
            raise ValueError("Interaction model has an incompatible feature basis")
        if payload.get("support_feature_names") != list(INTERACTION_SUPPORT_NAMES):
            raise ValueError("Interaction model has an incompatible support basis")
        try:
            return cls(
                intercept_log_ratio=float(payload["intercept_log_ratio"]),
                coefficients=tuple(float(value) for value in payload["coefficients"]),
                feature_means=tuple(float(value) for value in payload["feature_means"]),
                feature_scales=tuple(float(value) for value in payload["feature_scales"]),
                support_minima=tuple(float(value) for value in payload["support_minima"]),
                support_maxima=tuple(float(value) for value in payload["support_maxima"]),
                ridge_lambda=float(payload["ridge_lambda"]),
                uncertainty_residual_quantile=float(payload["uncertainty_residual_quantile"]),
                uncertainty_threshold=float(payload["uncertainty_threshold"]),
                training_family_count=int(payload["training_family_count"]),
                source_run_ids=tuple(str(value) for value in payload["source_run_ids"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed interaction Mask model artifact") from error


@dataclass(frozen=True, slots=True)
class InteractionMaskDecision:
    """Auditable hard-constraint, uncertainty, and ranking decision."""

    placement: MaskPlacement
    model_placement: MaskPlacement | None
    fallback_placement: MaskPlacement | None
    reason_code: str
    predicted_log_early_late_ratio: float | None
    uncertainty_threshold: float
    within_training_support: bool
    used_fallback: bool
    direct_model_decision: bool
    estimated_early_raw_exposure_rows: int
    estimated_late_raw_exposure_rows: int


def choose_mask_placement_by_interaction_cost(
    features: MaskPlacementFeatures,
    model: InteractionMaskCostModel,
) -> InteractionMaskDecision:
    """Apply governance constraints before uncertainty and cost ranking."""

    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")

    if early_feasible and not late_feasible:
        return InteractionMaskDecision(
            MaskPlacement.EARLY,
            None,
            None,
            "MASK_INTERACTION_LATE_INFEASIBLE",
            None,
            model.uncertainty_threshold,
            model.is_within_training_support(features),
            False,
            False,
            0,
            features.join_input_rows,
        )
    if late_feasible and not early_feasible:
        return InteractionMaskDecision(
            MaskPlacement.LATE,
            None,
            None,
            "MASK_INTERACTION_EARLY_INFEASIBLE",
            None,
            model.uncertainty_threshold,
            model.is_within_training_support(features),
            False,
            False,
            0,
            features.join_input_rows,
        )

    prediction = model.predict_log_early_late_ratio(features)
    model_placement = MaskPlacement.EARLY if prediction < 0.0 else MaskPlacement.LATE
    fallback = choose_mask_placement(features)
    within_support = model.is_within_training_support(features)
    if not within_support:
        placement = fallback.placement
        reason = "MASK_INTERACTION_OUT_OF_SUPPORT_FALLBACK"
        used_fallback = True
    elif abs(prediction) <= model.uncertainty_threshold:
        placement = fallback.placement
        reason = "MASK_INTERACTION_UNCERTAIN_FALLBACK"
        used_fallback = True
    else:
        placement = model_placement
        reason = "MASK_INTERACTION_CONFIDENT_COST_RANKING"
        used_fallback = False
    return InteractionMaskDecision(
        placement,
        model_placement,
        fallback.placement,
        reason,
        prediction,
        model.uncertainty_threshold,
        within_support,
        used_fallback,
        not used_fallback,
        0,
        features.join_input_rows,
    )


def choose_mask_placement_by_stable_interaction_cost(
    features: MaskPlacementFeatures,
    primary_model: InteractionMaskCostModel,
    stability_models: tuple[InteractionMaskCostModel, ...],
) -> InteractionMaskDecision:
    """Require unanimous ridge-model direction before a direct decision.

    Governance feasibility is resolved before evaluating any cost model.  The
    stability ensemble uses the same training families and fixed feature basis;
    only the frozen ridge coefficient differs between members.
    """

    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")
    if not early_feasible or not late_feasible:
        # The single-candidate path returns before any stability-model call.
        return choose_mask_placement_by_interaction_cost(features, primary_model)
    if not stability_models:
        raise ValueError("Stable interaction ranking requires at least one ridge model")

    predictions = tuple(model.predict_log_early_late_ratio(features) for model in stability_models)
    placements = tuple(
        MaskPlacement.EARLY if prediction < 0.0 else MaskPlacement.LATE
        for prediction in predictions
    )
    if len(set(placements)) != 1:
        prediction = primary_model.predict_log_early_late_ratio(features)
        fallback = choose_mask_placement(features)
        return InteractionMaskDecision(
            fallback.placement,
            MaskPlacement.EARLY if prediction < 0.0 else MaskPlacement.LATE,
            fallback.placement,
            "MASK_INTERACTION_RIDGE_DISAGREEMENT_FALLBACK",
            prediction,
            primary_model.uncertainty_threshold,
            primary_model.is_within_training_support(features),
            True,
            False,
            0,
            features.join_input_rows,
        )
    return choose_mask_placement_by_interaction_cost(features, primary_model)
