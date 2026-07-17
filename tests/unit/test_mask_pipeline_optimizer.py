"""Tests for the complete-fragment pipeline-aware Mask cost model."""

from __future__ import annotations

import pytest

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_pipeline import (
    PipelineMaskCostModel,
    choose_mask_placement_by_pipeline_cost,
    pipeline_cost_feature_vector,
)


def _model(*, uncertainty_margin: float = 0.0) -> PipelineMaskCostModel:
    # A positive materialization coefficient makes late Mask cheaper whenever
    # uncertainty and hard governance constraints do not override the ranking.
    return PipelineMaskCostModel(
        intercept_log_ms=5.0,
        coefficients=(0.0, 0.0, 0.0, 0.0, 1.0),
        feature_means=(0.0, 0.0, 0.0, 0.0, 0.0),
        feature_scales=(1.0, 1.0, 1.0, 1.0, 1.0),
        support_minima=(0.0, 0.0, 0.0),
        support_maxima=(10.0, 10.0, 1.0),
        ridge_lambda=0.1,
        paired_log_ratio_rmse=uncertainty_margin,
        uncertainty_multiplier=1.0,
        training_family_count=10,
        source_run_ids=("run",),
    )


def _features(**overrides: object) -> MaskPlacementFeatures:
    values: dict[str, object] = {
        "join_input_rows": 100_000,
        "identifier_width_bytes": 256,
        "join_match_rate": 1.0,
    }
    values.update(overrides)
    return MaskPlacementFeatures(**values)  # type: ignore[arg-type]


def test_physical_features_distinguish_early_and_late_work() -> None:
    features = _features(join_match_rate=0.25)
    early = pipeline_cost_feature_vector(features, MaskPlacement.EARLY)
    late = pipeline_cost_feature_vector(features, MaskPlacement.LATE)

    assert early[1] > late[1]  # Early hashes every input row.
    assert early[2] < late[2]  # Early sends fixed-width hashes into Join.
    assert early[4] == 1.0
    assert late[4] == 0.0


def test_confident_pipeline_ranking_selects_predicted_candidate() -> None:
    decision = choose_mask_placement_by_pipeline_cost(_features(), _model())

    assert decision.model_placement is MaskPlacement.LATE
    assert decision.placement is MaskPlacement.LATE
    assert decision.used_fallback is False
    assert decision.reason_code == "MASK_PIPELINE_CONFIDENT_COST_RANKING"


def test_uncertain_ranking_uses_frozen_v1_fallback() -> None:
    decision = choose_mask_placement_by_pipeline_cost(
        _features(), _model(uncertainty_margin=2.0)
    )

    assert decision.used_fallback is True
    assert decision.placement is decision.fallback_placement
    assert decision.reason_code == "MASK_PIPELINE_UNCERTAIN_FALLBACK"


def test_raw_exposure_is_a_hard_constraint_even_when_late_is_cheaper() -> None:
    decision = choose_mask_placement_by_pipeline_cost(
        _features(max_raw_exposure_rows=0), _model()
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_PIPELINE_LATE_INFEASIBLE"


def test_no_legal_candidate_fails_closed() -> None:
    with pytest.raises(ValueError, match="No legal Mask placement"):
        choose_mask_placement_by_pipeline_cost(
            _features(early_mask_legal=False, late_mask_legal=False), _model()
        )


def test_model_artifact_round_trip_preserves_prediction() -> None:
    original = _model()
    restored = PipelineMaskCostModel.from_dict(original.to_dict())

    assert restored.predict_log_early_late_ratio(_features()) == pytest.approx(
        original.predict_log_early_late_ratio(_features())
    )
