"""Tests for the regret-aware Mask residual decision layer."""

from __future__ import annotations

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_cost import DecomposedMaskCostModel
from trustaero.optimizer.mask_residual import (
    MASK_RESIDUAL_FEATURE_NAMES,
    RegretAwareMaskResidualModel,
    choose_mask_placement_with_residual,
    mask_residual_feature_vector,
)


def _model(*, residual_intercept: float = -2.0) -> RegretAwareMaskResidualModel:
    base = DecomposedMaskCostModel(
        intercept_log_ms=0.0,
        coefficients=(0.0, 1.0, 0.0, 0.0),
        ridge_lambda=0.1,
        training_candidate_count=20,
    )
    size = len(MASK_RESIDUAL_FEATURE_NAMES)
    return RegretAwareMaskResidualModel(
        base_model=base,
        residual_intercept=residual_intercept,
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


def test_residual_model_round_trip_and_exposes_two_stage_score() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.5,
    )
    model = _model()

    restored = RegretAwareMaskResidualModel.from_dict(model.to_dict())
    decision = choose_mask_placement_with_residual(features, restored)

    assert restored == model
    assert decision.corrected_log_early_late_ratio == (
        decision.base_log_early_late_ratio + decision.residual_correction
    )
    assert decision.placement is MaskPlacement.EARLY
    assert decision.used_base_fallback is False


def test_low_confidence_flip_retains_auditable_base_choice() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
    )
    base_only = _model(residual_intercept=0.0)
    base_ratio = base_only.predict_base_log_ratio(features)
    # Cross zero by less than one residual RMSE, which must not overturn the
    # physical base estimate on an uncertain statistical correction.
    model = _model(residual_intercept=-base_ratio - 0.01)

    decision = choose_mask_placement_with_residual(features, model)

    assert decision.used_base_fallback is True
    assert decision.reason_code == "MASK_RESIDUAL_BASE_FALLBACK_LOW_CONFIDENCE_FLIP"
    assert decision.decision_log_early_late_ratio == base_ratio


def test_governance_exposure_limit_overrides_residual_ranking() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
        max_raw_exposure_rows=0,
    )

    decision = choose_mask_placement_with_residual(
        features, _model(residual_intercept=10.0)
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_RESIDUAL_LATE_INFEASIBLE"


def test_feature_basis_uses_continuous_values_not_observed_thresholds() -> None:
    lower = mask_residual_feature_vector(
        MaskPlacementFeatures(
            join_input_rows=275_000,
            identifier_width_bytes=639,
            join_match_rate=0.35,
        )
    )
    upper = mask_residual_feature_vector(
        MaskPlacementFeatures(
            join_input_rows=275_000,
            identifier_width_bytes=640,
            join_match_rate=0.35,
        )
    )

    assert lower != upper
    assert abs(upper[2] - lower[2]) < 0.01
