"""Nested-calibrated local regret guard for Mask placement.

The guard does not learn another placement boundary.  It compares the local
out-of-fold regret of the residual selector with the frozen V1 selector and
uses the selector that was safer in nearby, independently predicted scenario
families.
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
from trustaero.optimizer.mask_residual import (
    RegretAwareMaskResidualModel,
    choose_mask_placement_with_residual,
)

MASK_GUARD_FEATURE_NAMES = (
    "log_input_rows",
    "log_identifier_width",
    "join_match_rate",
)


def mask_guard_feature_vector(features: MaskPlacementFeatures) -> tuple[float, ...]:
    """Describe local workload geometry without categorical scenario labels."""

    return (
        math.log1p(features.join_input_rows / 100_000.0),
        math.log1p(features.identifier_width_bytes / 64.0),
        features.join_match_rate,
    )


@dataclass(frozen=True)
class LocalRegretCalibrationPoint:
    """One inner-fold prediction used only for uncertainty calibration."""

    scenario_group_id: str
    workload_id: str
    feature_vector: tuple[float, ...]
    residual_regret_fraction: float
    v1_regret_fraction: float

    def __post_init__(self) -> None:
        if len(self.feature_vector) != len(MASK_GUARD_FEATURE_NAMES):
            raise ValueError("Local regret calibration point has incompatible features")
        if self.residual_regret_fraction < 0.0 or self.v1_regret_fraction < 0.0:
            raise ValueError("Local regret calibration values must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_group_id": self.scenario_group_id,
            "workload_id": self.workload_id,
            "feature_vector": list(self.feature_vector),
            "residual_regret_fraction": self.residual_regret_fraction,
            "v1_regret_fraction": self.v1_regret_fraction,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LocalRegretCalibrationPoint:
        try:
            return cls(
                scenario_group_id=str(payload["scenario_group_id"]),
                workload_id=str(payload["workload_id"]),
                feature_vector=tuple(float(value) for value in payload["feature_vector"]),
                residual_regret_fraction=float(payload["residual_regret_fraction"]),
                v1_regret_fraction=float(payload["v1_regret_fraction"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed local regret calibration point") from error


@dataclass(frozen=True)
class LocalRegretGuardModel:
    """Serializable nearest-scenario guard around a residual selector."""

    residual_model: RegretAwareMaskResidualModel
    calibration_points: tuple[LocalRegretCalibrationPoint, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    neighbor_group_count: int = 3

    def __post_init__(self) -> None:
        size = len(MASK_GUARD_FEATURE_NAMES)
        if len(self.feature_means) != size or len(self.feature_scales) != size:
            raise ValueError("Local regret guard has incompatible feature scalers")
        if any(scale <= 0.0 for scale in self.feature_scales):
            raise ValueError("Local regret guard feature scales must be positive")
        group_count = len({point.scenario_group_id for point in self.calibration_points})
        if self.neighbor_group_count <= 0 or group_count < self.neighbor_group_count:
            raise ValueError("Local regret guard lacks enough calibration scenario groups")

    def standardized_distance(
        self,
        left: tuple[float, ...],
        right: tuple[float, ...],
    ) -> float:
        """Compute scale-normalized Euclidean distance for two workloads."""

        if len(left) != len(self.feature_scales) or len(right) != len(self.feature_scales):
            raise ValueError("Local regret guard distance received incompatible features")
        return math.sqrt(
            sum(
                ((left_value - right_value) / scale) ** 2
                for left_value, right_value, scale in zip(
                    left,
                    right,
                    self.feature_scales,
                    strict=True,
                )
            )
        )

    def local_regret_estimates(
        self,
        features: MaskPlacementFeatures,
    ) -> tuple[float, float, tuple[str, ...], tuple[float, ...]]:
        """Estimate selector regret from the nearest distinct scenario groups."""

        query = mask_guard_feature_vector(features)
        grouped: dict[str, list[LocalRegretCalibrationPoint]] = {}
        for point in self.calibration_points:
            grouped.setdefault(point.scenario_group_id, []).append(point)
        ranked: list[tuple[float, str, float, float]] = []
        for group_id, points in grouped.items():
            distance = min(
                self.standardized_distance(query, point.feature_vector) for point in points
            )
            residual_regret = sum(point.residual_regret_fraction for point in points) / len(points)
            v1_regret = sum(point.v1_regret_fraction for point in points) / len(points)
            ranked.append((distance, group_id, residual_regret, v1_regret))
        neighbors = sorted(ranked)[: self.neighbor_group_count]
        return (
            sum(item[2] for item in neighbors) / len(neighbors),
            sum(item[3] for item in neighbors) / len(neighbors),
            tuple(item[1] for item in neighbors),
            tuple(item[0] for item in neighbors),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "local_regret_guard_v1",
            "feature_names": list(MASK_GUARD_FEATURE_NAMES),
            "residual_model": self.residual_model.to_dict(),
            "calibration_points": [point.to_dict() for point in self.calibration_points],
            "feature_means": list(self.feature_means),
            "feature_scales": list(self.feature_scales),
            "neighbor_group_count": self.neighbor_group_count,
            "selector_policy": "lower_unweighted_mean_regret_over_nearest_scenario_groups",
            "tie_break": "frozen_v1",
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LocalRegretGuardModel:
        if payload.get("model_type") != "local_regret_guard_v1":
            raise ValueError("Unsupported local regret guard model_type")
        if payload.get("feature_names") != list(MASK_GUARD_FEATURE_NAMES):
            raise ValueError("Local regret guard artifact has incompatible features")
        try:
            residual_payload = payload["residual_model"]
            points_payload = payload["calibration_points"]
            if not isinstance(residual_payload, dict) or not isinstance(points_payload, list):
                raise TypeError("Local regret guard nested artifacts are malformed")
            points: list[LocalRegretCalibrationPoint] = []
            for point_payload in points_payload:
                if not isinstance(point_payload, dict):
                    raise TypeError("Local regret calibration point must be an object")
                points.append(LocalRegretCalibrationPoint.from_dict(point_payload))
            return cls(
                residual_model=RegretAwareMaskResidualModel.from_dict(residual_payload),
                calibration_points=tuple(points),
                feature_means=tuple(float(value) for value in payload["feature_means"]),
                feature_scales=tuple(float(value) for value in payload["feature_scales"]),
                neighbor_group_count=int(payload["neighbor_group_count"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed local regret guard artifact") from error


@dataclass(frozen=True)
class LocalRegretGuardDecision:
    """Final choice plus the local evidence that selected its ranking method."""

    placement: MaskPlacement
    selected_selector: str
    reason_code: str
    residual_placement: MaskPlacement
    v1_placement: MaskPlacement
    estimated_local_residual_regret: float
    estimated_local_v1_regret: float
    neighbor_group_ids: tuple[str, ...]
    neighbor_distances: tuple[float, ...]


def choose_mask_placement_with_local_guard(
    features: MaskPlacementFeatures,
    model: LocalRegretGuardModel,
) -> LocalRegretGuardDecision:
    """Choose between residual ranking and V1 using nested local evidence."""

    residual = choose_mask_placement_with_residual(features, model.residual_model)
    v1 = choose_mask_placement(features)
    residual_regret, v1_regret, group_ids, distances = model.local_regret_estimates(features)
    if residual.placement is v1.placement:
        placement = residual.placement
        selector = "agreement"
        reason = "MASK_LOCAL_GUARD_SELECTORS_AGREE"
    elif residual_regret < v1_regret:
        placement = residual.placement
        selector = "residual"
        reason = "MASK_LOCAL_GUARD_RESIDUAL_LOWER_LOCAL_REGRET"
    else:
        placement = v1.placement
        selector = "v1"
        reason = "MASK_LOCAL_GUARD_V1_LOWER_OR_EQUAL_LOCAL_REGRET"
    return LocalRegretGuardDecision(
        placement=placement,
        selected_selector=selector,
        reason_code=reason,
        residual_placement=residual.placement,
        v1_placement=v1.placement,
        estimated_local_residual_regret=residual_regret,
        estimated_local_v1_regret=v1_regret,
        neighbor_group_ids=group_ids,
        neighbor_distances=distances,
    )
