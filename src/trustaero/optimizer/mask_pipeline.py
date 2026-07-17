"""Pipeline-aware cost model for two already-legal Mask placements.

The model estimates complete fragment latency rather than adding isolated
operator microbenchmarks.  Every feature denotes a concrete amount of work in
the bounded ``hash Mask + equality Join + ordered output`` fragment.  Semantic
legality and raw-value exposure are checked before a cost ranking is used.
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

PIPELINE_COST_FEATURE_NAMES = (
    "log_scan_payload_mib",
    "log_hash_input_payload_mib",
    "log_join_input_payload_mib",
    "log_join_output_rows_100k",
    "early_materialization_boundary",
)
PIPELINE_SUPPORT_FEATURE_NAMES = (
    "log_input_rows_100k",
    "log_identifier_width_ratio",
    "join_match_rate",
)


def pipeline_cost_feature_vector(
    features: MaskPlacementFeatures,
    placement: MaskPlacement,
    *,
    hashed_identifier_width_bytes: int = 64,
) -> tuple[float, ...]:
    """Translate a candidate into auditable physical-work features.

    Early Mask hashes every raw identifier, sends the fixed-width hash into
    Join, and introduces one materialization boundary.  Late Mask sends the
    raw identifier through Join and hashes only matched rows.  ``log1p`` keeps
    the formula continuous across scales without memorizing grid thresholds.
    """

    if hashed_identifier_width_bytes <= 0:
        raise ValueError("hashed_identifier_width_bytes must be positive")
    rows = float(features.join_input_rows)
    matched_rows = rows * features.join_match_rate
    raw_width = float(features.identifier_width_bytes)
    mib = float(1024 * 1024)
    scan_payload_mib = rows * raw_width / mib
    if placement is MaskPlacement.EARLY:
        hash_input_mib = scan_payload_mib
        join_input_mib = rows * hashed_identifier_width_bytes / mib
        materialization = 1.0
    else:
        hash_input_mib = matched_rows * raw_width / mib
        join_input_mib = scan_payload_mib
        materialization = 0.0
    return (
        math.log1p(scan_payload_mib),
        math.log1p(hash_input_mib),
        math.log1p(join_input_mib),
        math.log1p(matched_rows / 100_000.0),
        materialization,
    )


def pipeline_support_feature_vector(
    features: MaskPlacementFeatures,
) -> tuple[float, ...]:
    """Describe the workload domain used to reject unsafe extrapolation."""

    return (
        math.log1p(features.join_input_rows / 100_000.0),
        math.log1p(features.identifier_width_bytes / 64.0),
        features.join_match_rate,
    )


@dataclass(frozen=True)
class PipelineMaskCostModel:
    """Serializable non-negative model of complete candidate latency."""

    intercept_log_ms: float
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    support_minima: tuple[float, ...]
    support_maxima: tuple[float, ...]
    ridge_lambda: float
    paired_log_ratio_rmse: float
    uncertainty_multiplier: float
    training_family_count: int
    source_run_ids: tuple[str, ...]
    hashed_identifier_width_bytes: int = 64

    def __post_init__(self) -> None:
        feature_count = len(PIPELINE_COST_FEATURE_NAMES)
        if not (
            len(self.coefficients)
            == len(self.feature_means)
            == len(self.feature_scales)
            == feature_count
        ):
            raise ValueError(f"Pipeline Mask model requires {feature_count} cost features")
        support_count = len(PIPELINE_SUPPORT_FEATURE_NAMES)
        if not (
            len(self.support_minima) == len(self.support_maxima) == support_count
        ):
            raise ValueError(
                f"Pipeline Mask model requires {support_count} support features"
            )
        if any(value < 0.0 for value in self.coefficients):
            raise ValueError("Pipeline cost coefficients must be non-negative")
        if any(value <= 0.0 for value in self.feature_scales):
            raise ValueError("Pipeline feature scales must be positive")
        if any(
            lower > upper
            for lower, upper in zip(
                self.support_minima, self.support_maxima, strict=True
            )
        ):
            raise ValueError("Pipeline support bounds are invalid")
        if (
            self.ridge_lambda < 0.0
            or self.paired_log_ratio_rmse < 0.0
            or self.uncertainty_multiplier < 0.0
        ):
            raise ValueError("Pipeline fitting and uncertainty values must be non-negative")
        if self.training_family_count <= 0 or not self.source_run_ids:
            raise ValueError("Pipeline model must retain its training provenance")
        if self.hashed_identifier_width_bytes <= 0:
            raise ValueError("hashed_identifier_width_bytes must be positive")

    @property
    def uncertainty_margin(self) -> float:
        """Return the required absolute log-ratio before trusting a ranking."""

        return self.paired_log_ratio_rmse * self.uncertainty_multiplier

    def predict_log_latency_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> float:
        """Predict log milliseconds for one legal physical candidate."""

        raw = pipeline_cost_feature_vector(
            features,
            placement,
            hashed_identifier_width_bytes=self.hashed_identifier_width_bytes,
        )
        standardized = tuple(
            (value - mean) / scale
            for value, mean, scale in zip(
                raw, self.feature_means, self.feature_scales, strict=True
            )
        )
        return self.intercept_log_ms + sum(
            coefficient * value
            for coefficient, value in zip(
                self.coefficients, standardized, strict=True
            )
        )

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

    def is_within_training_support(self, features: MaskPlacementFeatures) -> bool:
        """Require every base workload dimension to remain inside training bounds."""

        values = pipeline_support_feature_vector(features)
        return all(
            lower <= value <= upper
            for value, lower, upper in zip(
                values, self.support_minima, self.support_maxima, strict=True
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "pipeline_mask_cost_v2",
            "formula_schema_version": 1,
            "target": "log_complete_fragment_latency_ms",
            "feature_names": list(PIPELINE_COST_FEATURE_NAMES),
            "support_feature_names": list(PIPELINE_SUPPORT_FEATURE_NAMES),
            "intercept_log_ms": self.intercept_log_ms,
            "coefficients": list(self.coefficients),
            "coefficient_constraint": "non_negative",
            "feature_means": list(self.feature_means),
            "feature_scales": list(self.feature_scales),
            "support_minima": list(self.support_minima),
            "support_maxima": list(self.support_maxima),
            "ridge_lambda": self.ridge_lambda,
            "paired_log_ratio_rmse": self.paired_log_ratio_rmse,
            "uncertainty_multiplier": self.uncertainty_multiplier,
            "training_family_count": self.training_family_count,
            "source_run_ids": list(self.source_run_ids),
            "hashed_identifier_width_bytes": self.hashed_identifier_width_bytes,
            "scientific_boundary": (
                "Only candidates already proven legal may be ranked; uncertainty "
                "falls back to the frozen governance-aware V1 selector."
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PipelineMaskCostModel:
        if payload.get("model_type") != "pipeline_mask_cost_v2":
            raise ValueError("Unsupported pipeline Mask cost model_type")
        if payload.get("feature_names") != list(PIPELINE_COST_FEATURE_NAMES):
            raise ValueError("Pipeline model has an incompatible cost feature basis")
        if payload.get("support_feature_names") != list(
            PIPELINE_SUPPORT_FEATURE_NAMES
        ):
            raise ValueError("Pipeline model has an incompatible support feature basis")
        try:
            return cls(
                intercept_log_ms=float(payload["intercept_log_ms"]),
                coefficients=tuple(float(value) for value in payload["coefficients"]),
                feature_means=tuple(float(value) for value in payload["feature_means"]),
                feature_scales=tuple(
                    float(value) for value in payload["feature_scales"]
                ),
                support_minima=tuple(
                    float(value) for value in payload["support_minima"]
                ),
                support_maxima=tuple(
                    float(value) for value in payload["support_maxima"]
                ),
                ridge_lambda=float(payload["ridge_lambda"]),
                paired_log_ratio_rmse=float(payload["paired_log_ratio_rmse"]),
                uncertainty_multiplier=float(payload["uncertainty_multiplier"]),
                training_family_count=int(payload["training_family_count"]),
                source_run_ids=tuple(str(value) for value in payload["source_run_ids"]),
                hashed_identifier_width_bytes=int(
                    payload["hashed_identifier_width_bytes"]
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed pipeline Mask cost artifact") from error


@dataclass(frozen=True)
class PipelineMaskCostDecision:
    """Auditable ranking, uncertainty state, and hard-constraint outcome."""

    placement: MaskPlacement
    model_placement: MaskPlacement
    fallback_placement: MaskPlacement
    reason_code: str
    estimated_early_latency_ms: float
    estimated_late_latency_ms: float
    predicted_log_early_late_ratio: float
    uncertainty_margin: float
    within_training_support: bool
    used_fallback: bool
    estimated_early_raw_exposure_rows: int
    estimated_late_raw_exposure_rows: int


def choose_mask_placement_by_pipeline_cost(
    features: MaskPlacementFeatures,
    model: PipelineMaskCostModel,
) -> PipelineMaskCostDecision:
    """Apply hard governance constraints before cost and uncertainty ranking."""

    early_ms = model.predict_latency_ms(features, MaskPlacement.EARLY)
    late_ms = model.predict_latency_ms(features, MaskPlacement.LATE)
    log_ratio = math.log(early_ms / late_ms)
    model_placement = (
        MaskPlacement.EARLY if log_ratio < 0.0 else MaskPlacement.LATE
    )
    fallback = choose_mask_placement(features)

    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")

    within_support = model.is_within_training_support(features)
    used_fallback = False
    if early_feasible and not late_feasible:
        placement = MaskPlacement.EARLY
        reason = "MASK_PIPELINE_LATE_INFEASIBLE"
    elif late_feasible and not early_feasible:
        placement = MaskPlacement.LATE
        reason = "MASK_PIPELINE_EARLY_INFEASIBLE"
    elif not within_support:
        placement = fallback.placement
        used_fallback = True
        reason = "MASK_PIPELINE_OUT_OF_SUPPORT_FALLBACK"
    elif abs(log_ratio) <= model.uncertainty_margin:
        placement = fallback.placement
        used_fallback = True
        reason = "MASK_PIPELINE_UNCERTAIN_FALLBACK"
    else:
        placement = model_placement
        reason = "MASK_PIPELINE_CONFIDENT_COST_RANKING"

    return PipelineMaskCostDecision(
        placement=placement,
        model_placement=model_placement,
        fallback_placement=fallback.placement,
        reason_code=reason,
        estimated_early_latency_ms=early_ms,
        estimated_late_latency_ms=late_ms,
        predicted_log_early_late_ratio=log_ratio,
        uncertainty_margin=model.uncertainty_margin,
        within_training_support=within_support,
        used_fallback=used_fallback,
        estimated_early_raw_exposure_rows=0,
        estimated_late_raw_exposure_rows=features.join_input_rows,
    )
