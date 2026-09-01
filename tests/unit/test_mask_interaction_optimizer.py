"""Tests for the bounded-interaction Mask optimizer."""

from __future__ import annotations

import pytest

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_interaction import (
    INTERACTION_FEATURE_NAMES,
    INTERACTION_SUPPORT_NAMES,
    InteractionMaskCostModel,
    choose_mask_placement_by_interaction_cost,
    choose_mask_placement_by_stable_interaction_cost,
    interaction_feature_vector,
)


def _model(*, threshold: float = 0.0) -> InteractionMaskCostModel:
    feature_count = len(INTERACTION_FEATURE_NAMES)
    return InteractionMaskCostModel(
        intercept_log_ratio=-0.2,
        coefficients=(0.0,) * feature_count,
        feature_means=(0.0,) * feature_count,
        feature_scales=(1.0,) * feature_count,
        support_minima=(0.0,) * len(INTERACTION_SUPPORT_NAMES),
        support_maxima=(10.0,) * len(INTERACTION_SUPPORT_NAMES),
        ridge_lambda=0.1,
        uncertainty_residual_quantile=0.5,
        uncertainty_threshold=threshold,
        training_family_count=20,
        source_run_ids=("fixture",),
    )


def test_feature_basis_is_fixed_and_continuous() -> None:
    first = interaction_feature_vector(MaskPlacementFeatures(100_000, 256, 0.5))
    second = interaction_feature_vector(MaskPlacementFeatures(100_001, 257, 0.5001))

    assert len(first) == len(INTERACTION_FEATURE_NAMES)
    assert first != second
    assert max(abs(left - right) for left, right in zip(first, second, strict=True)) < 0.02


def test_governance_removes_late_before_cost_ranking() -> None:
    decision = choose_mask_placement_by_interaction_cost(
        MaskPlacementFeatures(100_000, 256, 1.0, max_raw_exposure_rows=0),
        _model(),
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.model_placement is None
    assert decision.direct_model_decision is False
    assert decision.reason_code == "MASK_INTERACTION_LATE_INFEASIBLE"


def test_no_legal_candidate_fails_closed() -> None:
    with pytest.raises(ValueError, match="No legal Mask placement"):
        choose_mask_placement_by_interaction_cost(
            MaskPlacementFeatures(
                100_000,
                256,
                1.0,
                early_mask_legal=False,
                late_mask_legal=False,
            ),
            _model(),
        )


def test_uncertain_prediction_uses_frozen_v1_fallback() -> None:
    decision = choose_mask_placement_by_interaction_cost(
        MaskPlacementFeatures(100_000, 256, 1.0),
        _model(threshold=0.3),
    )

    assert decision.used_fallback is True
    assert decision.direct_model_decision is False
    assert decision.reason_code == "MASK_INTERACTION_UNCERTAIN_FALLBACK"


def test_model_round_trip_preserves_prediction() -> None:
    model = _model()
    restored = InteractionMaskCostModel.from_dict(model.to_dict())
    features = MaskPlacementFeatures(150_000, 512, 0.9)

    assert restored.predict_log_early_late_ratio(features) == pytest.approx(
        model.predict_log_early_late_ratio(features)
    )


def test_ridge_sign_disagreement_falls_back() -> None:
    primary = _model()
    opposing = InteractionMaskCostModel.from_dict({**primary.to_dict(), "intercept_log_ratio": 0.2})

    decision = choose_mask_placement_by_stable_interaction_cost(
        MaskPlacementFeatures(100_000, 256, 1.0),
        primary,
        (primary, opposing),
    )

    assert decision.used_fallback is True
    assert decision.direct_model_decision is False
    assert decision.reason_code == "MASK_INTERACTION_RIDGE_DISAGREEMENT_FALLBACK"


def test_unanimous_ridge_direction_allows_direct_decision() -> None:
    primary = _model()
    decision = choose_mask_placement_by_stable_interaction_cost(
        MaskPlacementFeatures(100_000, 256, 1.0),
        primary,
        (primary, primary),
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.direct_model_decision is True
