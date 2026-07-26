"""Tests for legality-first, dominance-safe hierarchical planning."""

from __future__ import annotations

import pytest

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.hierarchical_planner import (
    AUTHORIZED_RANKER_REQUIRED,
    CONSERVATIVE_FALLBACK,
    NO_LEGAL_CANDIDATE,
    ONLY_NONDOMINATED_CANDIDATE,
    GovernedCandidateProfile,
    HierarchicalPlannerConfig,
    hierarchical_planning_digest,
    plan_governed_candidates,
    prune_dominated_candidates,
)


def _profile(
    candidate_id: str,
    *,
    raw_rows: int,
    checkpoint: bool,
    hash_rows: float,
    checkpoint_bytes: float,
    equivalence_id: str = "governed-count-v1",
) -> GovernedCandidateProfile:
    """Build a compact profile with a common, auditable metric schema."""

    return GovernedCandidateProfile(
        candidate_id=candidate_id,
        result_equivalence_id=equivalence_id,
        exposure=CandidateExposure(
            candidate_id,
            raw_rows_exposed_to_join=0,
            raw_rows_materialized=raw_rows,
            provides_governance_checkpoint=checkpoint,
        ),
        work_metrics=(
            ("checkpoint.bytes", checkpoint_bytes),
            ("policy_hash.rows", hash_rows),
        ),
    )


def test_governance_filter_runs_before_dominance() -> None:
    """A non-checkpoint plan cannot dominate after the policy rejects it."""

    profiles = (
        _profile(
            "fused",
            raw_rows=0,
            checkpoint=False,
            hash_rows=10,
            checkpoint_bytes=0,
        ),
        _profile(
            "policy_first",
            raw_rows=0,
            checkpoint=True,
            hash_rows=100,
            checkpoint_bytes=100,
        ),
    )
    result = plan_governed_candidates(
        profiles,
        GovernanceFeasibilityPolicy(
            "checkpoint-required",
            None,
            None,
            require_governance_checkpoint=True,
        ),
    )

    assert result.status == "SELECT"
    assert result.selected_candidate_id == "policy_first"
    assert result.reason_code == ONLY_NONDOMINATED_CANDIDATE
    assert result.rejected_candidate_ids == ("fused",)
    assert result.dominated_candidate_ids == ()


def test_optional_checkpoint_prunes_mechanically_dominated_plan() -> None:
    profiles = (
        _profile(
            "fused",
            raw_rows=0,
            checkpoint=False,
            hash_rows=10,
            checkpoint_bytes=0,
        ),
        _profile(
            "materialized",
            raw_rows=20,
            checkpoint=True,
            hash_rows=10,
            checkpoint_bytes=1_000,
        ),
    )
    result = plan_governed_candidates(
        profiles,
        GovernanceFeasibilityPolicy("optional-checkpoint", None, None),
    )

    assert result.selected_candidate_id == "fused"
    assert result.dominated_candidate_ids == ("materialized",)
    proof = result.dominance_evidence[0]
    assert proof.dominator_candidate_id == "fused"
    assert "checkpoint.bytes" in proof.strictly_better_dimensions
    assert "exposure.raw_rows_materialized" in proof.strictly_better_dimensions


def test_tradeoff_uses_explicit_conservative_fallback() -> None:
    """Hash work and raw exposure trade off, so neither plan dominates."""

    profiles = (
        _profile(
            "policy_first",
            raw_rows=0,
            checkpoint=True,
            hash_rows=100,
            checkpoint_bytes=100,
        ),
        _profile(
            "query_first",
            raw_rows=20,
            checkpoint=True,
            hash_rows=20,
            checkpoint_bytes=500,
        ),
    )
    result = plan_governed_candidates(
        profiles,
        GovernanceFeasibilityPolicy(
            "checkpoint-required",
            None,
            None,
            require_governance_checkpoint=True,
        ),
        HierarchicalPlannerConfig(conservative_fallback_candidate_id="policy_first"),
    )

    assert result.status == "SELECT"
    assert result.selected_candidate_id == "policy_first"
    assert result.reason_code == CONSERVATIVE_FALLBACK
    assert result.nondominated_candidate_ids == ("policy_first", "query_first")
    assert result.performance_model_used is False


def test_multiple_survivors_defer_without_authorized_ranker() -> None:
    profiles = (
        _profile(
            "policy_first",
            raw_rows=0,
            checkpoint=True,
            hash_rows=100,
            checkpoint_bytes=100,
        ),
        _profile(
            "query_first",
            raw_rows=20,
            checkpoint=True,
            hash_rows=20,
            checkpoint_bytes=500,
        ),
    )
    result = plan_governed_candidates(
        profiles,
        GovernanceFeasibilityPolicy("permissive", None, None),
    )

    assert result.status == "DEFER"
    assert result.selected_candidate_id is None
    assert result.reason_code == AUTHORIZED_RANKER_REQUIRED


def test_empty_legal_set_is_fail_closed() -> None:
    result = plan_governed_candidates(
        (
            _profile(
                "raw_only",
                raw_rows=10,
                checkpoint=True,
                hash_rows=10,
                checkpoint_bytes=10,
            ),
        ),
        GovernanceFeasibilityPolicy("forbid-raw", None, 0),
    )

    assert result.status == "REJECT"
    assert result.reason_code == NO_LEGAL_CANDIDATE
    assert result.selected_candidate_id is None


def test_different_metric_schemas_and_results_are_not_compared() -> None:
    first = _profile(
        "first",
        raw_rows=0,
        checkpoint=True,
        hash_rows=1,
        checkpoint_bytes=1,
    )
    different_result = _profile(
        "different-result",
        raw_rows=10,
        checkpoint=True,
        hash_rows=10,
        checkpoint_bytes=10,
        equivalence_id="different-result",
    )
    different_metrics = GovernedCandidateProfile(
        candidate_id="different-metrics",
        result_equivalence_id="governed-count-v1",
        exposure=CandidateExposure("different-metrics", 0, 10),
        work_metrics=(("other.metric", 10.0),),
    )

    nondominated, dominated, evidence = prune_dominated_candidates(
        (first, different_result, different_metrics)
    )

    assert nondominated == ("first", "different-result", "different-metrics")
    assert dominated == ()
    assert evidence == ()


def test_invalid_profile_metadata_is_rejected() -> None:
    with pytest.raises(ValueError, match="bound to the same ID"):
        GovernedCandidateProfile(
            candidate_id="candidate",
            result_equivalence_id="result",
            exposure=CandidateExposure("other", 0, 0),
            work_metrics=(),
        )
    with pytest.raises(ValueError, match="sorted and unique"):
        GovernedCandidateProfile(
            candidate_id="candidate",
            result_equivalence_id="result",
            exposure=CandidateExposure("candidate", 0, 0),
            work_metrics=(("z", 0.0), ("a", 0.0)),
        )


def test_decision_digest_is_stable_and_covers_the_selected_candidate() -> None:
    profiles = (
        _profile(
            "policy_first",
            raw_rows=0,
            checkpoint=True,
            hash_rows=100,
            checkpoint_bytes=100,
        ),
        _profile(
            "query_first",
            raw_rows=20,
            checkpoint=True,
            hash_rows=20,
            checkpoint_bytes=500,
        ),
    )
    policy = GovernanceFeasibilityPolicy("permissive", None, None)
    policy_result = plan_governed_candidates(
        profiles,
        policy,
        HierarchicalPlannerConfig("policy_first"),
    )
    query_result = plan_governed_candidates(
        profiles,
        policy,
        HierarchicalPlannerConfig("query_first"),
    )

    first = hierarchical_planning_digest(policy_result)
    assert first == hierarchical_planning_digest(policy_result)
    assert first.startswith("sha256:")
    assert first != hierarchical_planning_digest(query_result)
