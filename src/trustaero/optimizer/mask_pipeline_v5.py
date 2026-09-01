"""Mechanism-prior plus pipeline-residual Mask optimizer V5.

V5 does not classify a workload as ``early`` or ``late`` directly.  It first
uses independently calibrated mechanism costs to estimate both candidates,
then applies a small residual surface for physical interactions absent from
isolated microbenchmarks (scan work, payload width, and a materialization
pipeline breaker).  Governance feasibility is resolved before either legal
candidate is ranked.

The model remains deliberately bounded to hash Mask placement around one
filtering many-to-one Join.  It must not be used as a generic SQL optimizer.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_mechanism import MechanismMaskCostModel

V5_RESIDUAL_FEATURE_NAMES = (
    "log_scan_payload_mib",
    "log_hash_input_payload_mib",
    "log_join_input_payload_mib",
    "log_join_output_rows_100k",
    "pipeline_breaker",
    "breaker_x_log_join_payload_mib",
)
V5_SUPPORT_FEATURE_NAMES = (
    "log_join_input_rows_100k",
    "log_identifier_width_ratio",
    "join_match_rate",
)


def v5_residual_feature_vector(
    features: MaskPlacementFeatures,
    placement: MaskPlacement,
    *,
    hashed_identifier_width_bytes: int = 64,
) -> tuple[float, ...]:
    """Return candidate-specific work missing from the mechanism prior."""

    if hashed_identifier_width_bytes <= 0:
        raise ValueError("hashed_identifier_width_bytes must be positive")
    rows = float(features.join_input_rows)
    matched_rows = rows * features.join_match_rate
    raw_width = float(features.identifier_width_bytes)
    mib = float(1024 * 1024)
    scan_payload = math.log1p(rows * raw_width / mib)
    if placement is MaskPlacement.EARLY:
        hash_payload = math.log1p(rows * raw_width / mib)
        join_payload = math.log1p(rows * hashed_identifier_width_bytes / mib)
        breaker = 1.0
    else:
        hash_payload = math.log1p(matched_rows * raw_width / mib)
        join_payload = math.log1p(rows * raw_width / mib)
        breaker = 0.0
    return (
        scan_payload,
        hash_payload,
        join_payload,
        math.log1p(matched_rows / 100_000.0),
        breaker,
        breaker * join_payload,
    )


def v5_support_feature_vector(
    features: MaskPlacementFeatures,
) -> tuple[float, ...]:
    """Describe the bounded domain used to reject extrapolation."""

    return (
        math.log1p(features.join_input_rows / 100_000.0),
        math.log1p(features.identifier_width_bytes / 64.0),
        features.join_match_rate,
    )


@dataclass(frozen=True, slots=True)
class PipelineV5ResidualSurface:
    """A small ridge residual model fitted on complete workload families."""

    intercept_log_ms: float
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    support_minima: tuple[float, ...]
    support_maxima: tuple[float, ...]
    residual_log_ratio_rmse: float
    uncertainty_multiplier: float
    ridge_lambda: float
    training_family_count: int
    source_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        cost_size = len(V5_RESIDUAL_FEATURE_NAMES)
        if not (
            len(self.coefficients)
            == len(self.feature_means)
            == len(self.feature_scales)
            == cost_size
        ):
            raise ValueError(f"V5 residual model requires {cost_size} features")
        support_size = len(V5_SUPPORT_FEATURE_NAMES)
        if not (len(self.support_minima) == len(self.support_maxima) == support_size):
            raise ValueError(f"V5 residual model requires {support_size} support axes")
        if any(scale <= 0.0 for scale in self.feature_scales):
            raise ValueError("V5 residual feature scales must be positive")
        if any(
            lower > upper
            for lower, upper in zip(self.support_minima, self.support_maxima, strict=True)
        ):
            raise ValueError("V5 support bounds are invalid")
        if (
            self.residual_log_ratio_rmse < 0.0
            or self.uncertainty_multiplier < 0.0
            or self.ridge_lambda <= 0.0
        ):
            raise ValueError("V5 residual fitting metadata is invalid")
        if self.training_family_count <= 0 or not self.source_run_ids:
            raise ValueError("V5 residual model must retain training provenance")

    @property
    def uncertainty_margin(self) -> float:
        return self.residual_log_ratio_rmse * self.uncertainty_multiplier

    def predict_residual_log_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
        *,
        hashed_identifier_width_bytes: int,
    ) -> float:
        vector = v5_residual_feature_vector(
            features,
            placement,
            hashed_identifier_width_bytes=hashed_identifier_width_bytes,
        )
        standardized = (
            (value - mean) / scale
            for value, mean, scale in zip(
                vector, self.feature_means, self.feature_scales, strict=True
            )
        )
        return self.intercept_log_ms + sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, standardized, strict=True)
        )

    def is_within_support(self, features: MaskPlacementFeatures) -> bool:
        values = v5_support_feature_vector(features)
        return all(
            lower <= value <= upper
            for value, lower, upper in zip(
                values, self.support_minima, self.support_maxima, strict=True
            )
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "model_type": "pipeline_v5_mechanism_residual_surface",
                "model_schema_version": 1,
                "feature_names": list(V5_RESIDUAL_FEATURE_NAMES),
                "support_feature_names": list(V5_SUPPORT_FEATURE_NAMES),
                "target": "log_observed_candidate_ms_minus_log_mechanism_prior_ms",
            }
        )
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PipelineV5ResidualSurface:
        if payload.get("model_type") != "pipeline_v5_mechanism_residual_surface":
            raise ValueError("Unsupported V5 residual model type")
        if payload.get("feature_names") != list(V5_RESIDUAL_FEATURE_NAMES):
            raise ValueError("V5 residual feature contract changed")
        if payload.get("support_feature_names") != list(V5_SUPPORT_FEATURE_NAMES):
            raise ValueError("V5 support feature contract changed")
        try:
            return cls(
                intercept_log_ms=float(payload["intercept_log_ms"]),
                coefficients=tuple(float(item) for item in payload["coefficients"]),
                feature_means=tuple(float(item) for item in payload["feature_means"]),
                feature_scales=tuple(float(item) for item in payload["feature_scales"]),
                support_minima=tuple(float(item) for item in payload["support_minima"]),
                support_maxima=tuple(float(item) for item in payload["support_maxima"]),
                residual_log_ratio_rmse=float(payload["residual_log_ratio_rmse"]),
                uncertainty_multiplier=float(payload["uncertainty_multiplier"]),
                ridge_lambda=float(payload["ridge_lambda"]),
                training_family_count=int(payload["training_family_count"]),
                source_run_ids=tuple(str(item) for item in payload["source_run_ids"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed V5 residual model") from error


@dataclass(frozen=True, slots=True)
class PipelineV5HybridModel:
    """Serializable mechanism prior plus complete-pipeline residual surface."""

    mechanism_prior: MechanismMaskCostModel
    residual_surface: PipelineV5ResidualSurface

    def predict_log_latency_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> float:
        prior_ms = self.mechanism_prior.predict_candidate_ms(features, placement)
        if prior_ms <= 0.0:
            raise ValueError("V5 mechanism prior produced a non-positive cost")
        residual = self.residual_surface.predict_residual_log_ms(
            features,
            placement,
            hashed_identifier_width_bytes=(self.mechanism_prior.hashed_identifier_width_bytes),
        )
        return math.log(prior_ms) + residual

    def predict_latency_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> float:
        return math.exp(self.predict_log_latency_ms(features, placement))

    def predict_log_early_late_ratio(self, features: MaskPlacementFeatures) -> float:
        return self.predict_log_latency_ms(
            features, MaskPlacement.EARLY
        ) - self.predict_log_latency_ms(features, MaskPlacement.LATE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "pipeline_mask_cost_v5_hybrid",
            "model_schema_version": 1,
            "mechanism_prior": self.mechanism_prior.to_dict(),
            "residual_surface": self.residual_surface.to_dict(),
            "governance_before_ranking": True,
            "uncertain_fallback": "early_mask",
            "direct_classifier_used": False,
            "scientific_boundary": (
                "Only two validated Mask placements around the bounded filtering "
                "many-to-one Join fragment are supported."
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PipelineV5HybridModel:
        if payload.get("model_type") != "pipeline_mask_cost_v5_hybrid":
            raise ValueError("Unsupported V5 hybrid model type")
        mechanism = payload.get("mechanism_prior")
        residual = payload.get("residual_surface")
        if not isinstance(mechanism, dict) or not isinstance(residual, dict):
            raise ValueError("V5 hybrid components must be JSON objects")
        return cls(
            mechanism_prior=MechanismMaskCostModel.from_dict(mechanism),
            residual_surface=PipelineV5ResidualSurface.from_dict(residual),
        )


@dataclass(frozen=True, slots=True)
class PipelineV5Decision:
    """Auditable V5 result after hard feasibility and uncertainty checks."""

    placement: MaskPlacement
    reason_code: str
    estimated_early_latency_ms: float | None
    estimated_late_latency_ms: float | None
    predicted_log_early_late_ratio: float | None
    uncertainty_margin: float
    within_training_support: bool
    direct_cost_decision: bool
    used_conservative_fallback: bool
    estimated_raw_join_exposure_rows: int


def choose_mask_placement_v5(
    features: MaskPlacementFeatures,
    model: PipelineV5HybridModel,
) -> PipelineV5Decision:
    """Exclude illegal candidates, then apply bounded cost and uncertainty."""

    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies governance")
    within = model.residual_surface.is_within_support(features)
    margin = model.residual_surface.uncertainty_margin
    # A forced legal choice is not credited as optimizer cost-selection skill.
    if early_feasible and not late_feasible:
        return PipelineV5Decision(
            MaskPlacement.EARLY,
            "MASK_V5_LATE_INFEASIBLE",
            None,
            None,
            None,
            margin,
            within,
            False,
            False,
            0,
        )
    if late_feasible and not early_feasible:
        return PipelineV5Decision(
            MaskPlacement.LATE,
            "MASK_V5_EARLY_INFEASIBLE",
            None,
            None,
            None,
            margin,
            within,
            False,
            False,
            features.join_input_rows,
        )

    early_ms = model.predict_latency_ms(features, MaskPlacement.EARLY)
    late_ms = model.predict_latency_ms(features, MaskPlacement.LATE)
    log_ratio = math.log(early_ms / late_ms)
    if not within:
        return PipelineV5Decision(
            MaskPlacement.EARLY,
            "MASK_V5_OUT_OF_SUPPORT_CONSERVATIVE_EARLY",
            early_ms,
            late_ms,
            log_ratio,
            margin,
            False,
            False,
            True,
            0,
        )
    if abs(log_ratio) <= margin:
        return PipelineV5Decision(
            MaskPlacement.EARLY,
            "MASK_V5_UNCERTAIN_CONSERVATIVE_EARLY",
            early_ms,
            late_ms,
            log_ratio,
            margin,
            True,
            False,
            True,
            0,
        )
    placement = MaskPlacement.EARLY if log_ratio < 0.0 else MaskPlacement.LATE
    return PipelineV5Decision(
        placement,
        "MASK_V5_CONFIDENT_COMPONENT_COST_RANKING",
        early_ms,
        late_ms,
        log_ratio,
        margin,
        True,
        True,
        False,
        0 if placement is MaskPlacement.EARLY else features.join_input_rows,
    )
