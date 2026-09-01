"""Tests for the nested-calibrated local regret guard."""

from __future__ import annotations

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_cost import DecomposedMaskCostModel
from trustaero.optimizer.mask_guard import (
    LocalRegretCalibrationPoint,
    LocalRegretGuardModel,
    choose_mask_placement_with_local_guard,
    mask_guard_feature_vector,
)
from trustaero.optimizer.mask_residual import (
    MASK_RESIDUAL_FEATURE_NAMES,
    RegretAwareMaskResidualModel,
)


def _residual_model() -> RegretAwareMaskResidualModel:
    base = DecomposedMaskCostModel(
        intercept_log_ms=0.0,
        coefficients=(0.0, 1.0, 0.0, 0.0),
        ridge_lambda=0.1,
        training_candidate_count=20,
    )
    size = len(MASK_RESIDUAL_FEATURE_NAMES)
    return RegretAwareMaskResidualModel(
        base_model=base,
        residual_intercept=-5.0,
        residual_coefficients=(0.0,) * size,
        feature_means=(0.0,) * size,
        feature_scales=(1.0,) * size,
        support_minima=(0.0,) * size,
        support_maxima=(100.0,) * size,
        ridge_lambda=0.1,
        weighted_residual_rmse=0.1,
        confidence_multiplier=1.0,
        training_sample_count=20,
        regret_weight_cap=10.0,
    )


def _guard() -> LocalRegretGuardModel:
    near_features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
    )
    far_features = MaskPlacementFeatures(
        join_input_rows=500_000,
        identifier_width_bytes=128,
        join_match_rate=0.9,
    )
    points = (
        LocalRegretCalibrationPoint(
            "near-a",
            "near-a/n100000",
            mask_guard_feature_vector(near_features),
            0.0,
            0.2,
        ),
        LocalRegretCalibrationPoint(
            "near-b",
            "near-b/n100000",
            mask_guard_feature_vector(near_features),
            0.01,
            0.1,
        ),
        LocalRegretCalibrationPoint(
            "far",
            "far/n500000",
            mask_guard_feature_vector(far_features),
            0.3,
            0.0,
        ),
    )
    return LocalRegretGuardModel(
        residual_model=_residual_model(),
        calibration_points=points,
        feature_means=(0.0, 0.0, 0.0),
        feature_scales=(1.0, 1.0, 1.0),
        neighbor_group_count=2,
    )


def test_local_guard_uses_nearest_distinct_scenario_regret() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
    )

    decision = choose_mask_placement_with_local_guard(features, _guard())

    assert decision.residual_placement is MaskPlacement.EARLY
    assert decision.v1_placement is MaskPlacement.LATE
    assert decision.placement is MaskPlacement.EARLY
    assert decision.selected_selector == "residual"
    assert decision.neighbor_group_ids == ("near-a", "near-b")


def test_local_guard_round_trip_preserves_calibration_evidence() -> None:
    model = _guard()

    restored = LocalRegretGuardModel.from_dict(model.to_dict())

    assert restored == model
