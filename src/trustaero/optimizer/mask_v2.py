"""Serializable, explainable latency-ratio model for Mask placement.

The model predicts ``log(early_latency / late_latency)`` from cardinality and
width estimates.  A negative prediction favors early Mask.  Semantic legality
and governance exposure remain hard constraints outside the learned score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures

MASK_V2_FEATURE_NAMES = (
    "join_input_rows_100k",
    "identifier_width_kib",
    "join_match_rate",
    "raw_input_work_100k_kib",
    "matched_work_100k_kib",
)


def mask_v2_feature_vector(features: MaskPlacementFeatures) -> tuple[float, ...]:
    """Convert optimizer estimates into the frozen V2 feature basis."""

    rows = features.join_input_rows / 100_000.0
    width = features.identifier_width_bytes / 1024.0
    raw_work = rows * width
    return (
        rows,
        width,
        features.join_match_rate,
        raw_work,
        raw_work * features.join_match_rate,
    )


@dataclass(frozen=True)
class MaskV2Model:
    """Standardized ridge model that can be inspected and serialized."""

    intercept: float
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    ridge_lambda: float
    training_sample_count: int

    def __post_init__(self) -> None:
        size = len(MASK_V2_FEATURE_NAMES)
        if not (
            len(self.coefficients) == len(self.feature_means) == len(self.feature_scales) == size
        ):
            raise ValueError(f"Mask V2 requires exactly {size} coefficients and scalers")
        if any(scale <= 0.0 for scale in self.feature_scales):
            raise ValueError("Mask V2 feature scales must be positive")
        if self.ridge_lambda < 0.0:
            raise ValueError("ridge_lambda must be non-negative")
        if self.training_sample_count <= 0:
            raise ValueError("training_sample_count must be positive")

    def predict_log_latency_ratio(self, features: MaskPlacementFeatures) -> float:
        """Predict log(early/late); lower than zero favors early Mask."""

        raw = mask_v2_feature_vector(features)
        standardized = tuple(
            (value - mean) / scale
            for value, mean, scale in zip(raw, self.feature_means, self.feature_scales, strict=True)
        )
        return self.intercept + sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, standardized, strict=True)
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible model artifact."""

        return {
            "model_type": "mask_latency_ratio_ridge_v2",
            "target": "log(early_mask_latency_ms / late_mask_latency_ms)",
            "feature_names": list(MASK_V2_FEATURE_NAMES),
            "intercept": self.intercept,
            "coefficients": list(self.coefficients),
            "feature_means": list(self.feature_means),
            "feature_scales": list(self.feature_scales),
            "ridge_lambda": self.ridge_lambda,
            "training_sample_count": self.training_sample_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MaskV2Model:
        """Load a model artifact while rejecting incompatible feature bases."""

        if payload.get("model_type") != "mask_latency_ratio_ridge_v2":
            raise ValueError("Unsupported Mask V2 model_type")
        if payload.get("feature_names") != list(MASK_V2_FEATURE_NAMES):
            raise ValueError("Mask V2 artifact has an incompatible feature basis")
        try:
            coefficients = tuple(float(value) for value in payload["coefficients"])
            means = tuple(float(value) for value in payload["feature_means"])
            scales = tuple(float(value) for value in payload["feature_scales"])
            return cls(
                intercept=float(payload["intercept"]),
                coefficients=coefficients,
                feature_means=means,
                feature_scales=scales,
                ridge_lambda=float(payload["ridge_lambda"]),
                training_sample_count=int(payload["training_sample_count"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed Mask V2 model artifact") from error


@dataclass(frozen=True)
class MaskV2Decision:
    """Auditable V2 decision after legality and exposure checks."""

    placement: MaskPlacement
    reason_code: str
    predicted_log_early_late_ratio: float
    predicted_early_late_ratio: float
    estimated_early_raw_exposure_rows: int
    estimated_late_raw_exposure_rows: int


def choose_mask_placement_v2(
    features: MaskPlacementFeatures,
    model: MaskV2Model,
) -> MaskV2Decision:
    """Choose a feasible placement without trading governance for speed."""

    prediction = model.predict_log_latency_ratio(features)
    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")
    if early_feasible and not late_feasible:
        placement = MaskPlacement.EARLY
        reason = "MASK_OPTIMIZER_V2_LATE_INFEASIBLE"
    elif late_feasible and not early_feasible:
        placement = MaskPlacement.LATE
        reason = "MASK_OPTIMIZER_V2_EARLY_INFEASIBLE"
    elif prediction < 0.0:
        placement = MaskPlacement.EARLY
        reason = "MASK_OPTIMIZER_V2_EARLY_PREDICTED_FASTER"
    else:
        placement = MaskPlacement.LATE
        reason = "MASK_OPTIMIZER_V2_LATE_PREDICTED_FASTER_OR_TIE"
    return MaskV2Decision(
        placement=placement,
        reason_code=reason,
        predicted_log_early_late_ratio=prediction,
        predicted_early_late_ratio=math.exp(prediction),
        estimated_early_raw_exposure_rows=0,
        estimated_late_raw_exposure_rows=features.join_input_rows,
    )
