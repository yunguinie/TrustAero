"""Tests for the explainable decomposed Mask candidate-cost model."""

from __future__ import annotations

import pytest

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_cost import (
    DecomposedMaskCostModel,
    choose_mask_placement_by_cost,
    mask_candidate_cost_features,
)


def _hash_dominated_model() -> DecomposedMaskCostModel:
    return DecomposedMaskCostModel(
        intercept_log_ms=0.0,
        coefficients=(0.0, 1.0, 0.0, 0.0),
        ridge_lambda=0.1,
        training_candidate_count=20,
    )


def test_candidate_features_represent_distinct_physical_work() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
    )

    early = mask_candidate_cost_features(features, MaskPlacement.EARLY)
    late = mask_candidate_cost_features(features, MaskPlacement.LATE)

    assert early[1] > late[1]  # Early hashes all rows, not only matches.
    assert early[2] < late[2]  # Early carries a narrow hash through the Join.
    assert early[3] > late[3]  # Only early uses an explicit materialization.


def test_decomposed_model_explains_and_selects_candidate_costs() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
    )

    decision = choose_mask_placement_by_cost(features, _hash_dominated_model())

    assert decision.placement is MaskPlacement.LATE
    assert decision.estimated_late_latency_ms < decision.estimated_early_latency_ms
    assert set(decision.early_components) == {
        "input_rows_log100k",
        "hash_input_log_mib",
        "join_payload_log_mib",
        "materialized_payload_log_mib",
    }


def test_governance_limit_remains_harder_than_estimated_cost() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
        max_raw_exposure_rows=0,
    )

    decision = choose_mask_placement_by_cost(features, _hash_dominated_model())

    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_COST_LATE_INFEASIBLE"


def test_decomposed_model_round_trip_and_nonnegative_validation() -> None:
    model = _hash_dominated_model()

    restored = DecomposedMaskCostModel.from_dict(model.to_dict())

    assert restored == model
    with pytest.raises(ValueError, match="non-negative"):
        DecomposedMaskCostModel(
            intercept_log_ms=0.0,
            coefficients=(0.0, -1.0, 0.0, 0.0),
            ridge_lambda=0.1,
            training_candidate_count=2,
        )
