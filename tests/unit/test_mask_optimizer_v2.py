"""Tests for the serializable Mask Optimizer V2 model."""

from __future__ import annotations

import pytest

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_v2 import MaskV2Model, choose_mask_placement_v2


def _constant_model(prediction: float) -> MaskV2Model:
    return MaskV2Model(
        intercept=prediction,
        coefficients=(0.0, 0.0, 0.0, 0.0, 0.0),
        feature_means=(0.0, 0.0, 0.0, 0.0, 0.0),
        feature_scales=(1.0, 1.0, 1.0, 1.0, 1.0),
        ridge_lambda=0.01,
        training_sample_count=10,
    )


def test_negative_latency_ratio_prediction_selects_early_mask() -> None:
    decision = choose_mask_placement_v2(
        MaskPlacementFeatures(
            join_input_rows=200_000,
            identifier_width_bytes=512,
            join_match_rate=1.0,
        ),
        _constant_model(-0.2),
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.predicted_early_late_ratio == pytest.approx(0.818730753)


def test_governance_exposure_limit_overrides_v2_latency_prediction() -> None:
    decision = choose_mask_placement_v2(
        MaskPlacementFeatures(
            join_input_rows=100,
            identifier_width_bytes=18,
            join_match_rate=0.1,
            max_raw_exposure_rows=0,
        ),
        _constant_model(2.0),
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_OPTIMIZER_V2_LATE_INFEASIBLE"


def test_v2_model_rejects_incomplete_feature_scalers() -> None:
    with pytest.raises(ValueError, match="exactly"):
        MaskV2Model(
            intercept=0.0,
            coefficients=(0.0,),
            feature_means=(0.0,),
            feature_scales=(1.0,),
            ridge_lambda=0.01,
            training_sample_count=1,
        )


def test_v2_model_artifact_round_trip_preserves_prediction() -> None:
    original = _constant_model(-0.2)
    restored = MaskV2Model.from_dict(original.to_dict())
    features = MaskPlacementFeatures(
        join_input_rows=200_000,
        identifier_width_bytes=512,
        join_match_rate=1.0,
    )

    assert restored.predict_log_latency_ratio(features) == pytest.approx(
        original.predict_log_latency_ratio(features)
    )
