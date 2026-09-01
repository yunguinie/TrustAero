"""Compact pipeline-aware ranking model for the bounded V4 Mask fragment."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import (
    RealPipelineWorkloadStats,
    candidate_work_delta,
)

PIPELINE_V4_MODEL_FEATURE_NAMES = (
    "delta_log_pre_join_hash_payload",
    "delta_log_post_join_hash_payload",
    "delta_log_join_fact_payload",
    "delta_log_boundary_payload",
)
_WORK_DELTA_INDICES = (2, 3, 4, 7)


def pipeline_v4_model_feature_vector(
    stats: RealPipelineWorkloadStats,
) -> tuple[float, ...]:
    """Select the four predeclared candidate-specific work differences."""

    delta = candidate_work_delta(stats)
    return tuple(delta[index] for index in _WORK_DELTA_INDICES)


@dataclass(frozen=True, slots=True)
class PipelineV4CostModel:
    """Serializable ridge surface with an explicit uncertainty boundary."""

    intercept_log_ratio: float
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    uncertainty_threshold: float
    ridge_lambda: float
    training_family_count: int
    training_scenario_groups: tuple[str, ...]
    support_join_input_rows: tuple[int, int]
    support_sensitive_width_bytes: tuple[float, float]
    support_match_rate: tuple[float, float]

    def __post_init__(self) -> None:
        size = len(PIPELINE_V4_MODEL_FEATURE_NAMES)
        if not (
            len(self.coefficients) == len(self.feature_means) == len(self.feature_scales) == size
        ):
            raise ValueError(f"Pipeline V4 requires exactly {size} model features")
        if any(item <= 0.0 for item in self.feature_scales):
            raise ValueError("Pipeline V4 feature scales must be positive")
        if self.uncertainty_threshold < 0.0 or self.ridge_lambda <= 0.0:
            raise ValueError("Pipeline V4 thresholds and ridge must be valid")
        if self.training_family_count <= 0 or not self.training_scenario_groups:
            raise ValueError("Pipeline V4 must retain training provenance")
        bounds = (
            self.support_join_input_rows,
            self.support_sensitive_width_bytes,
            self.support_match_rate,
        )
        if any(lower > upper for lower, upper in bounds):
            raise ValueError("Pipeline V4 support bounds are invalid")

    def predict_log_early_late_ratio(self, stats: RealPipelineWorkloadStats) -> float:
        vector = pipeline_v4_model_feature_vector(stats)
        return self.intercept_log_ratio + sum(
            coefficient * ((value - mean) / scale)
            for coefficient, value, mean, scale in zip(
                self.coefficients,
                vector,
                self.feature_means,
                self.feature_scales,
                strict=True,
            )
        )

    def is_within_support(self, stats: RealPipelineWorkloadStats) -> bool:
        return (
            self.support_join_input_rows[0]
            <= stats.join_input_rows
            <= self.support_join_input_rows[1]
            and self.support_sensitive_width_bytes[0]
            <= stats.sensitive_raw_width_bytes
            <= self.support_sensitive_width_bytes[1]
            and self.support_match_rate[0] <= stats.join_match_rate <= self.support_match_rate[1]
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "model_type": "pipeline_work_pairwise_ridge_v4",
                "model_schema_version": 1,
                "target": "paired_log_early_late_latency_ratio",
                "feature_names": list(PIPELINE_V4_MODEL_FEATURE_NAMES),
                "governance_before_ranking": True,
                "uncertain_fallback": "early_mask",
                "operator_profile_timings_are_features": False,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PipelineV4CostModel:
        if payload.get("model_type") != "pipeline_work_pairwise_ridge_v4":
            raise ValueError("Unsupported Pipeline V4 model type")
        if payload.get("feature_names") != list(PIPELINE_V4_MODEL_FEATURE_NAMES):
            raise ValueError("Pipeline V4 model feature contract changed")
        try:
            return cls(
                intercept_log_ratio=float(payload["intercept_log_ratio"]),
                coefficients=tuple(float(item) for item in payload["coefficients"]),
                feature_means=tuple(float(item) for item in payload["feature_means"]),
                feature_scales=tuple(float(item) for item in payload["feature_scales"]),
                uncertainty_threshold=float(payload["uncertainty_threshold"]),
                ridge_lambda=float(payload["ridge_lambda"]),
                training_family_count=int(payload["training_family_count"]),
                training_scenario_groups=tuple(
                    str(item) for item in payload["training_scenario_groups"]
                ),
                support_join_input_rows=tuple(
                    int(item) for item in payload["support_join_input_rows"]
                ),  # type: ignore[arg-type]
                support_sensitive_width_bytes=tuple(
                    float(item) for item in payload["support_sensitive_width_bytes"]
                ),  # type: ignore[arg-type]
                support_match_rate=tuple(float(item) for item in payload["support_match_rate"]),  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed Pipeline V4 model") from error


@dataclass(frozen=True, slots=True)
class PipelineV4Decision:
    """Auditable legal, direct, or conservative-fallback decision."""

    placement: MaskPlacement
    reason_code: str
    predicted_log_early_late_ratio: float | None
    within_support: bool
    direct_model_decision: bool
    used_conservative_fallback: bool
    estimated_raw_join_exposure_rows: int


def choose_mask_placement_v4(
    stats: RealPipelineWorkloadStats,
    model: PipelineV4CostModel,
) -> PipelineV4Decision:
    """Apply hard governance before bounded cost ranking."""

    early = stats.placement_is_legal(MaskPlacement.EARLY)
    late = stats.placement_is_legal(MaskPlacement.LATE)
    if not early and not late:
        raise ValueError("No legal Mask placement satisfies governance")
    if early and not late:
        return PipelineV4Decision(
            MaskPlacement.EARLY,
            "MASK_V4_LATE_INFEASIBLE",
            None,
            model.is_within_support(stats),
            False,
            False,
            0,
        )
    if late and not early:
        return PipelineV4Decision(
            MaskPlacement.LATE,
            "MASK_V4_EARLY_INFEASIBLE",
            None,
            model.is_within_support(stats),
            False,
            False,
            stats.join_input_rows,
        )
    prediction = model.predict_log_early_late_ratio(stats)
    within = model.is_within_support(stats)
    if not within:
        return PipelineV4Decision(
            MaskPlacement.EARLY,
            "MASK_V4_OUT_OF_SUPPORT_CONSERVATIVE_EARLY",
            prediction,
            False,
            False,
            True,
            0,
        )
    if abs(prediction) <= model.uncertainty_threshold:
        return PipelineV4Decision(
            MaskPlacement.EARLY,
            "MASK_V4_UNCERTAIN_CONSERVATIVE_EARLY",
            prediction,
            True,
            False,
            True,
            0,
        )
    placement = MaskPlacement.EARLY if prediction < 0.0 else MaskPlacement.LATE
    return PipelineV4Decision(
        placement,
        "MASK_V4_CONFIDENT_COST_RANKING",
        prediction,
        True,
        True,
        False,
        0 if placement == MaskPlacement.EARLY else stats.join_input_rows,
    )
