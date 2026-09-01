"""Integration tests for the three-candidate hierarchical planner."""

from __future__ import annotations

from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)
from trustaero.optimizer.governed_checkpoint_hierarchy import (
    FUSED_GOVERNED_PIPELINE,
    derive_checkpoint_hierarchy_profiles,
    plan_checkpoint_hierarchically,
)
from trustaero.optimizer.hierarchical_planner import (
    CONSERVATIVE_FALLBACK,
    ONLY_NONDOMINATED_CANDIDATE,
)


def _statistics() -> GovernedCheckpointStatistics:
    return GovernedCheckpointStatistics(
        input_rows=100_000,
        sensitive_width_bytes=640.0,
        estimated_policy_rows=30_000,
        estimated_query_rows=10_000,
        estimated_result_rows=3_000,
        statistic_provenance="catalog_estimate",
    )


def test_profiles_expose_raw_query_checkpoint_truthfully() -> None:
    profiles = {
        profile.candidate_id: profile
        for profile in derive_checkpoint_hierarchy_profiles(_statistics())
    }

    assert profiles[FUSED_GOVERNED_PIPELINE].exposure.raw_rows_materialized == 0
    assert profiles[FUSED_GOVERNED_PIPELINE].exposure.provides_governance_checkpoint is False
    assert profiles[POLICY_FIRST_CHECKPOINT].exposure.raw_rows_materialized == 0
    assert profiles[QUERY_FIRST_CHECKPOINT].exposure.raw_rows_materialized == 10_000


def test_optional_checkpoint_uses_fused_conservative_fallback() -> None:
    result = plan_checkpoint_hierarchically(
        _statistics(),
        GovernanceFeasibilityPolicy("optional", None, None),
    )

    assert result.status == "SELECT"
    assert result.selected_candidate_id == FUSED_GOVERNED_PIPELINE
    assert result.reason_code == CONSERVATIVE_FALLBACK
    assert result.performance_model_used is False
    # Policy-first is mechanically dominated, while query-first trades less
    # hash work for greater raw-materialization exposure.
    assert result.dominated_candidate_ids == (POLICY_FIRST_CHECKPOINT,)
    assert result.nondominated_candidate_ids == (
        FUSED_GOVERNED_PIPELINE,
        QUERY_FIRST_CHECKPOINT,
    )


def test_required_checkpoint_rejects_fused_then_falls_back_policy_first() -> None:
    result = plan_checkpoint_hierarchically(
        _statistics(),
        GovernanceFeasibilityPolicy(
            "checkpoint-required",
            None,
            None,
            require_governance_checkpoint=True,
        ),
    )

    assert result.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert result.reason_code == CONSERVATIVE_FALLBACK
    assert result.rejected_candidate_ids == (FUSED_GOVERNED_PIPELINE,)
    assert result.nondominated_candidate_ids == (
        POLICY_FIRST_CHECKPOINT,
        QUERY_FIRST_CHECKPOINT,
    )


def test_no_raw_checkpoint_prunes_to_fused_when_checkpoint_is_optional() -> None:
    result = plan_checkpoint_hierarchically(
        _statistics(),
        GovernanceFeasibilityPolicy("no-raw-checkpoint", None, 0),
    )

    assert result.selected_candidate_id == FUSED_GOVERNED_PIPELINE
    assert result.reason_code == ONLY_NONDOMINATED_CANDIDATE
    assert result.rejected_candidate_ids == (QUERY_FIRST_CHECKPOINT,)
    assert result.dominated_candidate_ids == (POLICY_FIRST_CHECKPOINT,)


def test_strict_required_checkpoint_has_only_policy_first() -> None:
    result = plan_checkpoint_hierarchically(
        _statistics(),
        GovernanceFeasibilityPolicy(
            "strict-checkpoint",
            None,
            0,
            require_governance_checkpoint=True,
        ),
    )

    assert result.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert result.reason_code == ONLY_NONDOMINATED_CANDIDATE
    assert set(result.rejected_candidate_ids) == {
        FUSED_GOVERNED_PIPELINE,
        QUERY_FIRST_CHECKPOINT,
    }
