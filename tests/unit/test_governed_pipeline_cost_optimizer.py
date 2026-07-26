"""Tests for legality-first deployment of the frozen pipeline cost model."""

from __future__ import annotations

from pathlib import Path

import pytest

from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_pipeline_cost import (
    FROZEN_V2_SUPPORT,
    MODEL_RANKED_LEGAL_CANDIDATE,
    ONLY_LEGAL_NONDOMINATED_CANDIDATE,
    OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK,
    FrozenGovernedPipelineCostModel,
    optimize_governed_pipeline,
)
from trustaero.optimizer.governed_pipeline_space import (
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    GovernedPipelineStatistics,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "experiments/frozen/models/governed_pipeline_cost_model_v2_20260724.json"
MODEL_SHA256 = "56399a09a3dccd2b02793eca97145fc17c68b9da8574edfef013925457f0fd6f"


def _model() -> FrozenGovernedPipelineCostModel:
    return FrozenGovernedPipelineCostModel.from_json(
        MODEL,
        expected_sha256=MODEL_SHA256,
    )


def _statistics(rows: int = 100_000) -> GovernedPipelineStatistics:
    return GovernedPipelineStatistics(
        input_rows=rows,
        estimated_policy_rows=20_000 if rows == 100_000 else rows // 5,
        estimated_query_rows=80_000 if rows == 100_000 else rows * 4 // 5,
        estimated_governed_rows=16_000 if rows == 100_000 else rows * 4 // 25,
        estimated_query_join_rows=64_000 if rows == 100_000 else rows * 16 // 25,
        estimated_result_rows=12_800 if rows == 100_000 else rows * 16 // 125,
        sensitive_width_bytes=768.0,
    )


def test_frozen_model_is_digest_bound() -> None:
    assert _model().model_sha256 == MODEL_SHA256
    with pytest.raises(ValueError, match="digest changed"):
        FrozenGovernedPipelineCostModel.from_json(
            MODEL,
            expected_sha256="0" * 64,
        )


def test_strict_policy_selects_only_legal_route_without_model() -> None:
    decision = optimize_governed_pipeline(
        _statistics(),
        GovernanceFeasibilityPolicy(
            "strict",
            0,
            0,
            require_governance_checkpoint=True,
        ),
        _model(),
    )

    assert decision.selected_candidate_id == POLICY_FIRST_MASKED_CHECKPOINT
    assert decision.reason_code == ONLY_LEGAL_NONDOMINATED_CANDIDATE
    assert decision.performance_model_used is False


def test_permissive_policy_ranks_only_legal_survivors() -> None:
    decision = optimize_governed_pipeline(
        _statistics(),
        GovernanceFeasibilityPolicy(
            "permissive",
            None,
            None,
            require_governance_checkpoint=True,
        ),
        _model(),
    )

    assert decision.reason_code == MODEL_RANKED_LEGAL_CANDIDATE
    assert decision.performance_model_used is True
    assert decision.selected_candidate_id in decision.feasible_candidate_ids
    assert len(decision.predicted_latency_ms) == 3


def test_raw_join_limit_prevents_join_first_selection() -> None:
    decision = optimize_governed_pipeline(
        _statistics(),
        GovernanceFeasibilityPolicy(
            "no-raw-join",
            0,
            None,
            require_governance_checkpoint=True,
        ),
        _model(),
    )

    assert JOIN_FIRST_MASKED_CHECKPOINT not in decision.feasible_candidate_ids
    assert decision.selected_candidate_id in decision.feasible_candidate_ids


def test_out_of_support_uses_conservative_fallback() -> None:
    decision = optimize_governed_pipeline(
        _statistics(rows=1_000_000),
        GovernanceFeasibilityPolicy(
            "permissive",
            None,
            None,
            require_governance_checkpoint=True,
        ),
        _model(),
        support=FROZEN_V2_SUPPORT,
    )

    assert decision.selected_candidate_id == POLICY_FIRST_MASKED_CHECKPOINT
    assert decision.reason_code == OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK
    assert decision.performance_model_used is False
    assert decision.out_of_support is True


def test_finite_sample_noise_does_not_trigger_false_fallback() -> None:
    """A 0.7 nominal rate may fluctuate slightly in a 120K-row sample."""

    statistics = GovernedPipelineStatistics(
        input_rows=120_000,
        estimated_policy_rows=84_419,
        estimated_query_rows=78_000,
        estimated_governed_rows=54_800,
        estimated_query_join_rows=64_000,
        estimated_result_rows=45_000,
        sensitive_width_bytes=768.0,
    )
    decision = optimize_governed_pipeline(
        statistics,
        GovernanceFeasibilityPolicy(
            "no-raw-join",
            0,
            None,
            require_governance_checkpoint=True,
        ),
        _model(),
    )

    assert statistics.estimated_policy_rows / statistics.input_rows > 0.7
    assert FROZEN_V2_SUPPORT.contains(statistics)
    assert decision.reason_code == MODEL_RANKED_LEGAL_CANDIDATE
    assert decision.performance_model_used is True


def test_materially_out_of_support_rate_still_fails_closed() -> None:
    """Statistical tolerance must not admit a genuinely shifted workload."""

    statistics = GovernedPipelineStatistics(
        input_rows=120_000,
        estimated_policy_rows=86_400,
        estimated_query_rows=78_000,
        estimated_governed_rows=56_000,
        estimated_query_join_rows=64_000,
        estimated_result_rows=46_000,
        sensitive_width_bytes=768.0,
    )
    decision = optimize_governed_pipeline(
        statistics,
        GovernanceFeasibilityPolicy(
            "no-raw-join",
            0,
            None,
            require_governance_checkpoint=True,
        ),
        _model(),
    )

    assert statistics.estimated_policy_rows / statistics.input_rows == 0.72
    assert not FROZEN_V2_SUPPORT.contains(statistics)
    assert decision.reason_code == OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK
