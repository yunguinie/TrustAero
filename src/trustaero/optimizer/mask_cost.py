"""Decomposed, non-negative cost model for early and late Mask plans.

Unlike a direct classifier, this model estimates each legal candidate from the
same operation coefficients. The features correspond to input scanning, hash
work, Join payload, and explicit materialization.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures

MASK_COST_FEATURE_NAMES = (
    "input_rows_log100k",
    "hash_input_log_mib",
    "join_payload_log_mib",
    "materialized_payload_log_mib",
)


def mask_candidate_cost_features(
    features: MaskPlacementFeatures,
    placement: MaskPlacement,
    *,
    hashed_identifier_width_bytes: int = 64,
) -> tuple[float, ...]:
    """Return work terms for one physical candidate.

    Early Mask hashes every input identifier, materializes its hash, and sends
    the narrow hash through the Join. Late Mask sends the raw identifier
    through the Join and hashes only estimated matching rows.
    """

    input_rows = float(features.join_input_rows)
    raw_width = float(features.identifier_width_bytes)
    hashed_width = float(hashed_identifier_width_bytes)
    if placement is MaskPlacement.EARLY:
        hash_bytes = input_rows * raw_width
        join_payload_bytes = input_rows * hashed_width
        materialized_bytes = input_rows * hashed_width
    else:
        hash_bytes = input_rows * features.join_match_rate * raw_width
        join_payload_bytes = input_rows * raw_width
        materialized_bytes = 0.0
    mib = float(1024 * 1024)
    return (
        math.log1p(input_rows / 100_000.0),
        math.log1p(hash_bytes / mib),
        math.log1p(join_payload_bytes / mib),
        math.log1p(materialized_bytes / mib),
    )


@dataclass(frozen=True)
class DecomposedMaskCostModel:
    """Shared non-negative operation coefficients over log latency."""

    intercept_log_ms: float
    coefficients: tuple[float, ...]
    ridge_lambda: float
    training_candidate_count: int
    hashed_identifier_width_bytes: int = 64

    def __post_init__(self) -> None:
        if len(self.coefficients) != len(MASK_COST_FEATURE_NAMES):
            raise ValueError("Decomposed Mask cost model has an incompatible feature count")
        if any(value < 0.0 for value in self.coefficients):
            raise ValueError("Decomposed Mask operation coefficients must be non-negative")
        if self.ridge_lambda < 0.0:
            raise ValueError("ridge_lambda must be non-negative")
        if self.training_candidate_count <= 0:
            raise ValueError("training_candidate_count must be positive")
        if self.hashed_identifier_width_bytes <= 0:
            raise ValueError("hashed_identifier_width_bytes must be positive")

    def component_contributions(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> dict[str, float]:
        """Expose every additive log-cost component for plan explanations."""

        values = mask_candidate_cost_features(
            features,
            placement,
            hashed_identifier_width_bytes=self.hashed_identifier_width_bytes,
        )
        return {
            name: coefficient * value
            for name, coefficient, value in zip(
                MASK_COST_FEATURE_NAMES,
                self.coefficients,
                values,
                strict=True,
            )
        }

    def predict_log_latency_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> float:
        """Estimate one candidate's log latency in milliseconds."""

        return self.intercept_log_ms + sum(
            self.component_contributions(features, placement).values()
        )

    def predict_latency_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> float:
        return math.exp(self.predict_log_latency_ms(features, placement))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "decomposed_mask_cost_v1",
            "target": "log(governed_latency_ms)",
            "feature_names": list(MASK_COST_FEATURE_NAMES),
            "intercept_log_ms": self.intercept_log_ms,
            "coefficients": list(self.coefficients),
            "ridge_lambda": self.ridge_lambda,
            "training_candidate_count": self.training_candidate_count,
            "hashed_identifier_width_bytes": self.hashed_identifier_width_bytes,
            "coefficient_constraint": "non_negative",
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DecomposedMaskCostModel:
        if payload.get("model_type") != "decomposed_mask_cost_v1":
            raise ValueError("Unsupported decomposed Mask cost model_type")
        if payload.get("feature_names") != list(MASK_COST_FEATURE_NAMES):
            raise ValueError("Decomposed Mask artifact has an incompatible feature basis")
        try:
            return cls(
                intercept_log_ms=float(payload["intercept_log_ms"]),
                coefficients=tuple(float(value) for value in payload["coefficients"]),
                ridge_lambda=float(payload["ridge_lambda"]),
                training_candidate_count=int(payload["training_candidate_count"]),
                hashed_identifier_width_bytes=int(payload["hashed_identifier_width_bytes"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed decomposed Mask cost artifact") from error


@dataclass(frozen=True)
class DecomposedMaskCostDecision:
    """Feasibility-aware choice with separate candidate estimates."""

    placement: MaskPlacement
    reason_code: str
    estimated_early_latency_ms: float
    estimated_late_latency_ms: float
    early_components: dict[str, float]
    late_components: dict[str, float]


def choose_mask_placement_by_cost(
    features: MaskPlacementFeatures,
    model: DecomposedMaskCostModel,
) -> DecomposedMaskCostDecision:
    """Filter infeasible plans before comparing their estimated costs."""

    early_ms = model.predict_latency_ms(features, MaskPlacement.EARLY)
    late_ms = model.predict_latency_ms(features, MaskPlacement.LATE)
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
        reason = "MASK_COST_LATE_INFEASIBLE"
    elif late_feasible and not early_feasible:
        placement = MaskPlacement.LATE
        reason = "MASK_COST_EARLY_INFEASIBLE"
    elif early_ms < late_ms:
        placement = MaskPlacement.EARLY
        reason = "MASK_COST_EARLY_ESTIMATED_CHEAPER"
    else:
        placement = MaskPlacement.LATE
        reason = "MASK_COST_LATE_ESTIMATED_CHEAPER_OR_TIE"
    return DecomposedMaskCostDecision(
        placement=placement,
        reason_code=reason,
        estimated_early_latency_ms=early_ms,
        estimated_late_latency_ms=late_ms,
        early_components=model.component_contributions(features, MaskPlacement.EARLY),
        late_components=model.component_contributions(features, MaskPlacement.LATE),
    )
