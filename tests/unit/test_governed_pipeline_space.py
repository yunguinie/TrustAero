"""Tests for the governance-driven multi-operator candidate space."""

from __future__ import annotations

import pytest

from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_pipeline_space import (
    FUSED_GOVERNED,
    GOVERNED_PIPELINE_CANDIDATE_IDS,
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    GovernedPipelineStatistics,
    build_governed_pipeline_candidates,
    plan_governed_pipeline,
)


def _statistics() -> GovernedPipelineStatistics:
    return GovernedPipelineStatistics(
        input_rows=100_000,
        estimated_policy_rows=30_000,
        estimated_query_rows=20_000,
        estimated_governed_rows=6_000,
        estimated_query_join_rows=12_000,
        estimated_result_rows=3_000,
        sensitive_width_bytes=640.0,
    )


def test_candidates_share_results_but_move_governance_work() -> None:
    candidates = build_governed_pipeline_candidates(_statistics())

    assert tuple(item.candidate_id for item in candidates) == (GOVERNED_PIPELINE_CANDIDATE_IDS)
    assert len({item.profile.result_equivalence_id for item in candidates}) == 1
    assert {item.checkpoint_kind for item in candidates} == {
        "none",
        "masked",
        "raw_then_masked",
        "raw_join_then_masked",
    }
    assert all(item.operator_order[-1] == "ProjectMaskedResult" for item in candidates)
    assert all("RecordLineage" in item.operator_order for item in candidates)


def test_exposure_metadata_distinguishes_raw_checkpoint_and_join() -> None:
    profiles = {
        item.candidate_id: item.profile
        for item in build_governed_pipeline_candidates(_statistics())
    }

    assert profiles[FUSED_GOVERNED].exposure.raw_rows_materialized == 0
    assert profiles[POLICY_FIRST_MASKED_CHECKPOINT].exposure.masked_rows_materialized == 30_000
    assert profiles[QUERY_FIRST_RAW_CHECKPOINT].exposure.raw_rows_materialized == 20_000
    assert profiles[QUERY_FIRST_RAW_CHECKPOINT].exposure.masked_rows_materialized == 6_000
    assert profiles[JOIN_FIRST_MASKED_CHECKPOINT].exposure.raw_rows_exposed_to_join == 20_000
    assert profiles[JOIN_FIRST_MASKED_CHECKPOINT].exposure.raw_rows_materialized == 12_000


def test_required_checkpoint_has_three_non_dominated_legal_candidates() -> None:
    result = plan_governed_pipeline(
        _statistics(),
        GovernanceFeasibilityPolicy(
            "checkpoint-required",
            None,
            None,
            require_governance_checkpoint=True,
        ),
    )

    assert result.rejected_candidate_ids == (FUSED_GOVERNED,)
    assert result.nondominated_candidate_ids == (
        POLICY_FIRST_MASKED_CHECKPOINT,
        QUERY_FIRST_RAW_CHECKPOINT,
        JOIN_FIRST_MASKED_CHECKPOINT,
    )
    assert result.selected_candidate_id == POLICY_FIRST_MASKED_CHECKPOINT
    assert result.performance_model_used is False


def test_exposure_policies_change_the_legal_candidate_space() -> None:
    statistics = _statistics()
    no_raw_checkpoint = plan_governed_pipeline(
        statistics,
        GovernanceFeasibilityPolicy(
            "no-raw-checkpoint",
            None,
            0,
            require_governance_checkpoint=True,
        ),
    )
    no_raw_join = plan_governed_pipeline(
        statistics,
        GovernanceFeasibilityPolicy(
            "no-raw-join",
            0,
            None,
            require_governance_checkpoint=True,
        ),
    )
    strict = plan_governed_pipeline(
        statistics,
        GovernanceFeasibilityPolicy(
            "strict",
            0,
            0,
            require_governance_checkpoint=True,
        ),
    )

    assert QUERY_FIRST_RAW_CHECKPOINT in no_raw_checkpoint.rejected_candidate_ids
    assert JOIN_FIRST_MASKED_CHECKPOINT in no_raw_join.rejected_candidate_ids
    assert strict.nondominated_candidate_ids == (POLICY_FIRST_MASKED_CHECKPOINT,)


def test_invalid_cross_stage_cardinality_is_rejected() -> None:
    with pytest.raises(ValueError, match="Final result rows"):
        GovernedPipelineStatistics(
            input_rows=100,
            estimated_policy_rows=50,
            estimated_query_rows=40,
            estimated_governed_rows=20,
            estimated_query_join_rows=10,
            estimated_result_rows=11,
            sensitive_width_bytes=128,
        )
