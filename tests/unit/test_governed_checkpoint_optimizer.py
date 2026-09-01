from __future__ import annotations

import pytest

from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.execution_aware import (
    AnalyticExecutionCostModel,
    AnalyticFeatureRate,
    FeatureSupportBound,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
    derive_governed_checkpoint_work,
    rank_governed_checkpoint_candidates,
)


def _statistics(query_rows: int) -> GovernedCheckpointStatistics:
    return GovernedCheckpointStatistics(
        input_rows=150_000,
        sensitive_width_bytes=1_024,
        estimated_policy_rows=15_000,
        estimated_query_rows=query_rows,
        estimated_result_rows=round(query_rows * 0.1),
        statistic_provenance="catalog_exact_controlled",
    )


def _model() -> AnalyticExecutionCostModel:
    works = [
        derive_governed_checkpoint_work(_statistics(7_500), candidate_id)
        for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
    ]
    names = sorted({name for work in works for name, _value in work.features})
    rates = {
        "policy_hash.input_gib": 1_000.0,
        "checkpoint.narrow_write_gib": 100.0,
        "checkpoint.raw_write_gib": 2_500.0,
        "checkpoint.post_rows_million": 0.0,
        "join.result_rows_million": 0.0,
    }
    return AnalyticExecutionCostModel(
        calibration_id="governed-checkpoint-test",
        rates=tuple(AnalyticFeatureRate(name, rates[name]) for name in names),
        support_bounds=tuple(FeatureSupportBound(name, 0.0, 10.0) for name in names),
        stable_legal_preference=(
            POLICY_FIRST_CHECKPOINT,
            QUERY_FIRST_CHECKPOINT,
        ),
    )


def test_work_vector_tracks_hash_and_checkpoint_bytes() -> None:
    statistics = _statistics(7_500)
    policy = derive_governed_checkpoint_work(statistics, POLICY_FIRST_CHECKPOINT).as_dict()
    query = derive_governed_checkpoint_work(statistics, QUERY_FIRST_CHECKPOINT).as_dict()

    assert policy["policy_hash.input_gib"] == pytest.approx(150_000 * 1_024 / 1024**3)
    assert query["policy_hash.input_gib"] == pytest.approx(7_500 * 1_024 / 1024**3)
    assert policy["checkpoint.raw_write_gib"] == 0.0
    assert query["checkpoint.raw_write_gib"] == pytest.approx(7_500 * (1_024 + 16) / 1024**3)


def test_analytic_cost_changes_choice_with_query_cardinality() -> None:
    permissive = GovernanceFeasibilityPolicy("permissive", None, None)

    low_query = rank_governed_checkpoint_candidates(_statistics(7_500), permissive, _model())
    high_query = rank_governed_checkpoint_candidates(_statistics(75_000), permissive, _model())

    assert low_query.selected_candidate_id == QUERY_FIRST_CHECKPOINT
    assert high_query.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert low_query.reason_code == "GOVERNED_CHECKPOINT_MINIMUM_ANALYTIC_COST"
    assert high_query.reason_code == "GOVERNED_CHECKPOINT_MINIMUM_ANALYTIC_COST"


def test_v41_practical_tie_keeps_minimum_cost_legal_candidate() -> None:
    """A performance tie must not override the cheaper governance-legal plan."""

    permissive = GovernanceFeasibilityPolicy("permissive", None, None)
    statistics = _statistics(42_000)

    frozen_v4 = rank_governed_checkpoint_candidates(
        statistics,
        permissive,
        _model(),
    )
    v41 = rank_governed_checkpoint_candidates(
        statistics,
        permissive,
        _model(),
        practical_tie_strategy="minimum_analytic_cost",
    )

    # The default is intentionally unchanged so old frozen results remain
    # reproducible.  V4.1 changes only the decision policy used for a legal tie.
    assert frozen_v4.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert frozen_v4.reason_code == "GOVERNED_CHECKPOINT_PRACTICAL_TIE_SAFE_FALLBACK"
    assert v41.selected_candidate_id == QUERY_FIRST_CHECKPOINT
    assert v41.reason_code == "GOVERNED_CHECKPOINT_PRACTICAL_TIE_MINIMUM_ANALYTIC_COST"
    assert v41.practically_tied_candidate_ids == frozen_v4.practically_tied_candidate_ids


def test_unknown_practical_tie_strategy_fails_closed() -> None:
    permissive = GovernanceFeasibilityPolicy("permissive", None, None)

    with pytest.raises(ValueError, match="Unknown practical-tie strategy"):
        rank_governed_checkpoint_candidates(
            _statistics(42_000),
            permissive,
            _model(),
            practical_tie_strategy="unknown",  # type: ignore[arg-type]
        )


def test_strict_policy_removes_raw_checkpoint_before_cost() -> None:
    strict = GovernanceFeasibilityPolicy("no-raw-checkpoint", None, 0)

    result = rank_governed_checkpoint_candidates(_statistics(7_500), strict, _model())

    assert result.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert result.reason_code == "GOVERNED_CHECKPOINT_ONLY_LEGAL_CANDIDATE"
    assert result.rejected_candidate_ids == (QUERY_FIRST_CHECKPOINT,)
    assert result.estimates == ()


def test_invalid_statistics_fail_closed() -> None:
    with pytest.raises(ValueError, match="exceed input rows"):
        GovernedCheckpointStatistics(
            input_rows=10,
            sensitive_width_bytes=128,
            estimated_policy_rows=11,
            estimated_query_rows=1,
            estimated_result_rows=1,
            statistic_provenance="catalog_estimate",
        )
