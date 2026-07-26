"""Tests for grouped one-sided checkpoint uncertainty protection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    analytic_model_from_dict,
)
from trustaero.experiments.governed_checkpoint_uncertainty_calibration import (
    grouped_one_sided_error_bound,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)
from trustaero.optimizer.governed_checkpoint_uncertainty import (
    QUERY_CONFIDENT,
    UNCERTAIN_FALLBACK,
    CheckpointUncertaintyGuard,
    rank_uncertainty_aware_checkpoint_candidates,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _guard() -> CheckpointUncertaintyGuard:
    payload = json.loads(
        (
            PROJECT_ROOT
            / "experiments/frozen/models/governed_checkpoint_optimizer_v2_20260723.json"
        ).read_text(encoding="utf-8")
    )
    return CheckpointUncertaintyGuard(
        base_model=analytic_model_from_dict(payload),
        query_margin_error_upper_ms=2.37896036460923,
        coverage=0.9,
        calibration_family_count=4,
    )


def _statistics(*, width: int, policy_rows: int, query_rows: int) -> GovernedCheckpointStatistics:
    return GovernedCheckpointStatistics(
        input_rows=150_000,
        sensitive_width_bytes=float(width),
        estimated_policy_rows=policy_rows,
        estimated_query_rows=query_rows,
        estimated_result_rows=round(policy_rows * query_rows / 150_000),
        statistic_provenance="catalog_exact_controlled",
    )


def test_grouped_bound_uses_one_score_per_family() -> None:
    decisions = []
    medians: dict[tuple[str, int, str], float] = {}
    family_cases = [
        (-1.0, 2.38),
        (-1.0, 1.51),
        (-1.0, 1.45),
        (-1.0, -1.45),
        (1.0, 5.29),
        (1.0, 2.18),
        (1.0, 1.99),
        (1.0, -3.12),
    ]
    for family_index, (predicted_margin, error) in enumerate(family_cases):
        scenario = f"family-{family_index}"
        for seed in (1, 2, 3):
            decisions.append(
                {
                    "scenario_id": scenario,
                    "seed": seed,
                    "estimated_costs_ms": {
                        POLICY_FIRST_CHECKPOINT: 10.0,
                        QUERY_FIRST_CHECKPOINT: 10.0 + predicted_margin,
                    },
                }
            )
            medians[(scenario, seed, POLICY_FIRST_CHECKPOINT)] = 10.0
            medians[(scenario, seed, QUERY_FIRST_CHECKPOINT)] = 10.0 + predicted_margin + error

    bound, families = grouped_one_sided_error_bound(decisions, medians, coverage=0.9)

    assert len(families) == 4
    assert bound == pytest.approx(2.38)


def test_small_predicted_advantage_falls_back_safely() -> None:
    decision = rank_uncertainty_aware_checkpoint_candidates(
        _statistics(width=256, policy_rows=60_000, query_rows=30_000),
        GovernanceFeasibilityPolicy("raw_permitted", None, None),
        _guard(),
    )

    assert decision.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert decision.reason_code == UNCERTAIN_FALLBACK


def test_clear_query_advantage_is_retained() -> None:
    decision = rank_uncertainty_aware_checkpoint_candidates(
        _statistics(width=768, policy_rows=60_000, query_rows=15_000),
        GovernanceFeasibilityPolicy("raw_permitted", None, None),
        _guard(),
    )

    assert decision.selected_candidate_id == QUERY_FIRST_CHECKPOINT
    assert decision.reason_code == QUERY_CONFIDENT


def test_governance_still_precedes_uncertainty() -> None:
    decision = rank_uncertainty_aware_checkpoint_candidates(
        _statistics(width=768, policy_rows=60_000, query_rows=15_000),
        GovernanceFeasibilityPolicy("raw_forbidden", None, 0),
        _guard(),
    )

    assert decision.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert decision.reason_code == "GOVERNED_CHECKPOINT_ONLY_LEGAL_CANDIDATE"
