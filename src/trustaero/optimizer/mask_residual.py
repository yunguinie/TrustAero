"""Regret-aware residual ranking for early and late Mask candidates.

The decomposed candidate-cost model remains the auditable foundation.  This
module learns only the paired log-ratio error left by that foundation, so an
optimizer decision can still show both the physical base estimate and the
smaller statistical correction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_cost import DecomposedMaskCostModel

MASK_RESIDUAL_FEATURE_NAMES = (
    "log_input_rows",
    "log_input_rows_squared",
    "log_width_ratio",
    "log_width_ratio_squared",
    "log_rows_width_interaction",
    "join_match_rate",
    "match_log_rows_interaction",
    "match_log_width_interaction",
    "match_log_rows_width_interaction",
)

# These raw features all increase with match rate. Constraining their fitted
# coefficients to be non-positive preserves the expected physical direction:
# a higher Join match rate must not make early Mask less attractive.
MATCH_MONOTONE_FEATURE_INDICES = (5, 6, 7, 8)


def mask_residual_feature_vector(features: MaskPlacementFeatures) -> tuple[float, ...]:
    """Return a continuous, knot-free residual feature basis.

    Log transforms express scale effects without memorizing observed widths or
    row-count thresholds.  The interaction terms allow the residual to model
    curvature that the shared base-cost coefficients cannot identify.
    """

    log_rows = math.log1p(features.join_input_rows / 100_000.0)
    log_width = math.log1p(features.identifier_width_bytes / 64.0)
    match_rate = features.join_match_rate
    return (
        log_rows,
        log_rows**2,
        log_width,
        log_width**2,
        log_rows * log_width,
        match_rate,
        match_rate * log_rows,
        match_rate * log_width,
        match_rate * log_rows * log_width,
    )


@dataclass(frozen=True)
class RegretAwareMaskResidualModel:
    """Serializable residual model layered over decomposed candidate costs."""

    base_model: DecomposedMaskCostModel
    residual_intercept: float
    residual_coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    support_minima: tuple[float, ...]
    support_maxima: tuple[float, ...]
    ridge_lambda: float
    weighted_residual_rmse: float
    confidence_multiplier: float
    training_sample_count: int
    regret_weight_cap: float

    def __post_init__(self) -> None:
        size = len(MASK_RESIDUAL_FEATURE_NAMES)
        sequences = (
            self.residual_coefficients,
            self.feature_means,
            self.feature_scales,
            self.support_minima,
            self.support_maxima,
        )
        if any(len(values) != size for values in sequences):
            raise ValueError(f"Mask residual model requires exactly {size} features")
        if any(scale <= 0.0 for scale in self.feature_scales):
            raise ValueError("Mask residual feature scales must be positive")
        if any(self.support_minima[index] > self.support_maxima[index] for index in range(size)):
            raise ValueError("Mask residual support bounds are invalid")
        if any(
            self.residual_coefficients[index] > 1e-12 for index in MATCH_MONOTONE_FEATURE_INDICES
        ):
            raise ValueError("Match-dependent residual coefficients must be non-positive")
        if self.ridge_lambda < 0.0 or self.weighted_residual_rmse < 0.0:
            raise ValueError("Mask residual fitting statistics must be non-negative")
        if self.confidence_multiplier < 0.0 or self.regret_weight_cap <= 0.0:
            raise ValueError("Mask residual confidence and weight cap are invalid")
        if self.training_sample_count <= 0:
            raise ValueError("training_sample_count must be positive")

    def predict_base_log_ratio(self, features: MaskPlacementFeatures) -> float:
        """Return log(early/late) from the explainable base-cost model."""

        early = self.base_model.predict_log_latency_ms(features, MaskPlacement.EARLY)
        late = self.base_model.predict_log_latency_ms(features, MaskPlacement.LATE)
        return early - late

    def predict_residual(self, features: MaskPlacementFeatures) -> float:
        """Predict only the paired error left by the base-cost formula."""

        raw = mask_residual_feature_vector(features)
        standardized = tuple(
            (value - mean) / scale
            for value, mean, scale in zip(raw, self.feature_means, self.feature_scales, strict=True)
        )
        return self.residual_intercept + sum(
            coefficient * value
            for coefficient, value in zip(
                self.residual_coefficients,
                standardized,
                strict=True,
            )
        )

    def predict_corrected_log_ratio(self, features: MaskPlacementFeatures) -> float:
        return self.predict_base_log_ratio(features) + self.predict_residual(features)

    def is_within_training_support(self, features: MaskPlacementFeatures) -> bool:
        """Reject statistical extrapolation beyond every observed raw feature."""

        raw = mask_residual_feature_vector(features)
        return all(
            lower <= value <= upper
            for value, lower, upper in zip(
                raw,
                self.support_minima,
                self.support_maxima,
                strict=True,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "regret_aware_mask_residual_v1",
            "target": "observed_log_early_late_ratio_minus_base_log_ratio",
            "base_model": self.base_model.to_dict(),
            "feature_names": list(MASK_RESIDUAL_FEATURE_NAMES),
            "match_monotone_feature_indices": list(MATCH_MONOTONE_FEATURE_INDICES),
            "residual_intercept": self.residual_intercept,
            "residual_coefficients": list(self.residual_coefficients),
            "feature_means": list(self.feature_means),
            "feature_scales": list(self.feature_scales),
            "support_minima": list(self.support_minima),
            "support_maxima": list(self.support_maxima),
            "ridge_lambda": self.ridge_lambda,
            "weighted_residual_rmse": self.weighted_residual_rmse,
            "confidence_multiplier": self.confidence_multiplier,
            "training_sample_count": self.training_sample_count,
            "regret_weight_cap": self.regret_weight_cap,
            "uncertainty_policy": ("retain_base_on_out_of_support_or_low_confidence_sign_flip"),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RegretAwareMaskResidualModel:
        if payload.get("model_type") != "regret_aware_mask_residual_v1":
            raise ValueError("Unsupported Mask residual model_type")
        if payload.get("feature_names") != list(MASK_RESIDUAL_FEATURE_NAMES):
            raise ValueError("Mask residual artifact has an incompatible feature basis")
        if payload.get("match_monotone_feature_indices") != list(MATCH_MONOTONE_FEATURE_INDICES):
            raise ValueError("Mask residual artifact has incompatible constraints")
        try:
            base_payload = payload["base_model"]
            if not isinstance(base_payload, dict):
                raise TypeError("base_model must be an object")
            return cls(
                base_model=DecomposedMaskCostModel.from_dict(base_payload),
                residual_intercept=float(payload["residual_intercept"]),
                residual_coefficients=tuple(
                    float(value) for value in payload["residual_coefficients"]
                ),
                feature_means=tuple(float(value) for value in payload["feature_means"]),
                feature_scales=tuple(float(value) for value in payload["feature_scales"]),
                support_minima=tuple(float(value) for value in payload["support_minima"]),
                support_maxima=tuple(float(value) for value in payload["support_maxima"]),
                ridge_lambda=float(payload["ridge_lambda"]),
                weighted_residual_rmse=float(payload["weighted_residual_rmse"]),
                confidence_multiplier=float(payload["confidence_multiplier"]),
                training_sample_count=int(payload["training_sample_count"]),
                regret_weight_cap=float(payload["regret_weight_cap"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed Mask residual model artifact") from error


@dataclass(frozen=True)
class RegretAwareMaskResidualDecision:
    """Auditable choice with base, residual, corrected, and fallback scores."""

    placement: MaskPlacement
    reason_code: str
    base_log_early_late_ratio: float
    residual_correction: float
    corrected_log_early_late_ratio: float
    decision_log_early_late_ratio: float
    within_training_support: bool
    used_base_fallback: bool


def choose_mask_placement_with_residual(
    features: MaskPlacementFeatures,
    model: RegretAwareMaskResidualModel,
) -> RegretAwareMaskResidualDecision:
    """Apply governance feasibility before a confidence-aware ranking choice."""

    base_ratio = model.predict_base_log_ratio(features)
    residual = model.predict_residual(features)
    corrected_ratio = base_ratio + residual
    within_support = model.is_within_training_support(features)
    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and features.join_input_rows <= features.max_raw_exposure_rows
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")

    used_fallback = False
    decision_ratio = corrected_ratio
    if early_feasible and not late_feasible:
        placement = MaskPlacement.EARLY
        reason = "MASK_RESIDUAL_LATE_INFEASIBLE"
    elif late_feasible and not early_feasible:
        placement = MaskPlacement.LATE
        reason = "MASK_RESIDUAL_EARLY_INFEASIBLE"
    else:
        signs_differ = (base_ratio < 0.0) != (corrected_ratio < 0.0)
        # A correction may be numerically large merely because the base score
        # was far from zero.  Confidence therefore concerns the corrected
        # score's distance from the decision boundary, not correction size.
        correction_is_confident = abs(corrected_ratio) > (
            model.confidence_multiplier * model.weighted_residual_rmse
        )
        if not within_support or (signs_differ and not correction_is_confident):
            used_fallback = True
            decision_ratio = base_ratio
            reason = (
                "MASK_RESIDUAL_BASE_FALLBACK_OUT_OF_SUPPORT"
                if not within_support
                else "MASK_RESIDUAL_BASE_FALLBACK_LOW_CONFIDENCE_FLIP"
            )
        elif corrected_ratio < 0.0:
            reason = "MASK_RESIDUAL_EARLY_PREDICTED_FASTER"
        else:
            reason = "MASK_RESIDUAL_LATE_PREDICTED_FASTER_OR_TIE"
        placement = MaskPlacement.EARLY if decision_ratio < 0.0 else MaskPlacement.LATE

    return RegretAwareMaskResidualDecision(
        placement=placement,
        reason_code=reason,
        base_log_early_late_ratio=base_ratio,
        residual_correction=residual,
        corrected_log_early_late_ratio=corrected_ratio,
        decision_log_early_late_ratio=decision_ratio,
        within_training_support=within_support,
        used_base_fallback=used_fallback,
    )
