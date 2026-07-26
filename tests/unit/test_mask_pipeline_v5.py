from __future__ import annotations

import pytest

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_mechanism import (
    HASH_FEATURE_NAMES,
    JOIN_FEATURE_NAMES,
    MATERIALIZATION_FEATURE_NAMES,
    MechanismMaskCostModel,
    NonnegativeMechanismCost,
)
from trustaero.optimizer.mask_pipeline_v5 import (
    PipelineV5HybridModel,
    PipelineV5ResidualSurface,
    choose_mask_placement_v5,
    v5_residual_feature_vector,
)


def _component(name: str, features: tuple[str, ...]) -> NonnegativeMechanismCost:
    return NonnegativeMechanismCost(
        component_name=name,
        feature_names=features,
        intercept_ms=1.0,
        coefficients=tuple(1.0 for _ in features),
        ridge_lambda=0.1,
        training_group_count=4,
        source_run_ids=("micro-run",),
    )


def _model(*, intercept: float = 0.0, rmse: float = 0.0) -> PipelineV5HybridModel:
    prior = MechanismMaskCostModel(
        hash_cost=_component("sha256", HASH_FEATURE_NAMES),
        materialization_cost=_component("materialization_roundtrip", MATERIALIZATION_FEATURE_NAMES),
        join_cost=_component("hash_join", JOIN_FEATURE_NAMES),
    )
    surface = PipelineV5ResidualSurface(
        intercept_log_ms=intercept,
        coefficients=(0.0,) * 6,
        feature_means=(0.0,) * 6,
        feature_scales=(1.0,) * 6,
        support_minima=(0.0, 0.0, 0.0),
        support_maxima=(10.0, 10.0, 1.0),
        residual_log_ratio_rmse=rmse,
        uncertainty_multiplier=1.0,
        ridge_lambda=0.1,
        training_family_count=20,
        source_run_ids=("pipeline-run",),
    )
    return PipelineV5HybridModel(prior, surface)


def test_residual_features_expose_candidate_specific_pipeline_work() -> None:
    features = MaskPlacementFeatures(100_000, 1024, 0.1)
    early = v5_residual_feature_vector(features, MaskPlacement.EARLY)
    late = v5_residual_feature_vector(features, MaskPlacement.LATE)
    assert early[1] > late[1]  # Early hashes all rows; late hashes matched rows.
    assert early[2] < late[2]  # Early carries fixed-width hashes through Join.
    assert early[4:] != late[4:]  # Only early introduces the bounded breaker.


def test_governance_forced_choice_does_not_run_cost_ranking() -> None:
    features = MaskPlacementFeatures(
        100_000,
        1024,
        0.1,
        max_raw_exposure_rows=0,
    )
    decision = choose_mask_placement_v5(features, _model())
    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_V5_LATE_INFEASIBLE"
    assert decision.estimated_early_latency_ms is None
    assert decision.direct_cost_decision is False


def test_uncertain_prediction_falls_back_conservatively() -> None:
    features = MaskPlacementFeatures(100_000, 64, 1.0)
    decision = choose_mask_placement_v5(features, _model(rmse=10.0))
    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_V5_UNCERTAIN_CONSERVATIVE_EARLY"
    assert decision.used_conservative_fallback is True


def test_model_round_trip_and_no_legal_candidate_error() -> None:
    model = _model()
    assert PipelineV5HybridModel.from_dict(model.to_dict()) == model
    with pytest.raises(ValueError, match="No legal Mask placement"):
        choose_mask_placement_v5(
            MaskPlacementFeatures(
                100,
                16,
                0.5,
                early_mask_legal=False,
                late_mask_legal=False,
            ),
            model,
        )
