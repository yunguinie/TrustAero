"""Tests for the optimizer's policy-before-cost candidate gate."""

from __future__ import annotations

import pytest

from trustaero.optimizer.candidate_feasibility import (
    RAW_JOIN_LIMIT_EXCEEDED,
    RAW_MATERIALIZATION_LIMIT_EXCEEDED,
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    evaluate_candidate_feasibility,
    filter_feasible_candidates,
)


def _four_way_exposures() -> tuple[CandidateExposure, ...]:
    """Mirror the four Phase 2M candidates without attaching any latency."""

    return (
        CandidateExposure("late_fused", 100, 0, 0),
        CandidateExposure("late_join_materialized", 100, 80, 0),
        CandidateExposure("late_hash_materialized", 100, 0, 80),
        CandidateExposure("early_hash_materialized", 0, 0, 100),
    )


def test_raw_permissive_policy_keeps_every_candidate() -> None:
    result = filter_feasible_candidates(
        _four_way_exposures(),
        GovernanceFeasibilityPolicy("raw_permissive", None, None),
    )

    assert result.status == "ACCEPT"
    assert result.feasible_candidate_ids == tuple(
        item.candidate_id for item in _four_way_exposures()
    )
    assert result.rejected_candidate_ids == ()


def test_raw_materialization_is_rejected_before_cost_ranking() -> None:
    result = filter_feasible_candidates(
        _four_way_exposures(),
        GovernanceFeasibilityPolicy("no_raw_materialization", None, 0),
    )

    assert result.feasible_candidate_ids == (
        "late_fused",
        "late_hash_materialized",
        "early_hash_materialized",
    )
    rejected = next(
        item for item in result.decisions if item.candidate_id == "late_join_materialized"
    )
    assert [item.code for item in rejected.diagnostics] == [RAW_MATERIALIZATION_LIMIT_EXCEEDED]
    assert rejected.diagnostics[0].observed_rows == 80
    assert rejected.diagnostics[0].allowed_rows == 0


def test_no_raw_join_policy_leaves_only_early_hash_candidate() -> None:
    result = filter_feasible_candidates(
        _four_way_exposures(),
        GovernanceFeasibilityPolicy("no_raw_join", 0, 0),
    )

    assert result.status == "ACCEPT"
    assert result.feasible_candidate_ids == ("early_hash_materialized",)
    joined_and_materialized = next(
        item for item in result.decisions if item.candidate_id == "late_join_materialized"
    )
    assert [item.code for item in joined_and_materialized.diagnostics] == [
        RAW_JOIN_LIMIT_EXCEEDED,
        RAW_MATERIALIZATION_LIMIT_EXCEEDED,
    ]


def test_bounded_policy_reports_observed_and_allowed_rows() -> None:
    decision = evaluate_candidate_feasibility(
        CandidateExposure("bounded_candidate", 51, 0),
        GovernanceFeasibilityPolicy("bounded", 50, 0),
    )

    assert decision.is_feasible is False
    assert decision.diagnostics[0].code == RAW_JOIN_LIMIT_EXCEEDED
    assert decision.diagnostics[0].observed_rows == 51
    assert decision.diagnostics[0].allowed_rows == 50


def test_empty_legal_set_returns_fail_closed_reject() -> None:
    result = filter_feasible_candidates(
        (CandidateExposure("raw_only", 1, 1),),
        GovernanceFeasibilityPolicy("deny_raw", 0, 0),
    )

    assert result.status == "REJECT"
    assert result.feasible_candidate_ids == ()
    assert result.rejected_candidate_ids == ("raw_only",)


def test_invalid_exposure_and_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        CandidateExposure("invalid", -1, 0)
    duplicate = (CandidateExposure("same", 0, 0), CandidateExposure("same", 0, 0))
    with pytest.raises(ValueError, match="must be unique"):
        filter_feasible_candidates(
            duplicate,
            GovernanceFeasibilityPolicy("policy", None, None),
        )
