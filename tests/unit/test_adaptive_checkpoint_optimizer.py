from __future__ import annotations

import pytest

from trustaero.optimizer.adaptive_checkpoint import (
    AdaptiveCheckpointConfig,
    PilotLatencyBlock,
    choose_adaptive_checkpoint,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)


def _statistics(query_rows: int = 30_000) -> GovernedCheckpointStatistics:
    return GovernedCheckpointStatistics(
        input_rows=100_000,
        sensitive_width_bytes=384.0,
        estimated_policy_rows=25_000,
        estimated_query_rows=query_rows,
        estimated_result_rows=min(25_000, query_rows),
        statistic_provenance="catalog_exact_controlled",
    )


def _blocks(policy_ms: float, query_ms: float) -> tuple[PilotLatencyBlock, ...]:
    return tuple(
        PilotLatencyBlock(index, policy_ms + index * 0.001, query_ms + index * 0.001)
        for index in range(10)
    )


def _config() -> AdaptiveCheckpointConfig:
    return AdaptiveCheckpointConfig(bootstrap_draws=500, bootstrap_seed=7)


def test_material_policy_winner_is_selected() -> None:
    result = choose_adaptive_checkpoint(
        _statistics(),
        GovernanceFeasibilityPolicy("permissive", None, None),
        _blocks(5.0, 10.0),
        _config(),
    )

    assert result.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert result.pilot_conclusion == "POLICY_FIRST_MATERIALLY_FASTER"
    assert result.governance_before_pilot is True


def test_material_query_winner_is_selected() -> None:
    result = choose_adaptive_checkpoint(
        _statistics(),
        GovernanceFeasibilityPolicy("permissive", None, None),
        _blocks(10.0, 5.0),
        _config(),
    )

    assert result.selected_candidate_id == QUERY_FIRST_CHECKPOINT
    assert result.pilot_conclusion == "QUERY_FIRST_MATERIALLY_FASTER"


def test_inconclusive_pilot_uses_frozen_threshold() -> None:
    result = choose_adaptive_checkpoint(
        _statistics(query_rows=34_000),
        GovernanceFeasibilityPolicy("permissive", None, None),
        _blocks(10.0, 10.0),
        _config(),
    )

    assert result.selected_candidate_id == QUERY_FIRST_CHECKPOINT
    assert result.pilot_conclusion == "INCONCLUSIVE"
    assert result.reason_code == "ADAPTIVE_CHECKPOINT_INCONCLUSIVE_FROZEN_BASELINE"


def test_strict_policy_skips_pilot_and_selects_only_legal_candidate() -> None:
    result = choose_adaptive_checkpoint(
        _statistics(),
        GovernanceFeasibilityPolicy("strict", None, 0),
        (),
        _config(),
    )

    assert result.selected_candidate_id == POLICY_FIRST_CHECKPOINT
    assert result.paired_block_count == 0
    assert result.pilot_cost_ms == 0.0


def test_piloting_rejected_candidate_fails_closed() -> None:
    with pytest.raises(ValueError, match="governance-illegal"):
        choose_adaptive_checkpoint(
            _statistics(),
            GovernanceFeasibilityPolicy("strict", None, 0),
            _blocks(5.0, 10.0),
            _config(),
        )


def test_invalid_or_insufficient_pilot_is_rejected() -> None:
    with pytest.raises(ValueError, match="insufficient"):
        choose_adaptive_checkpoint(
            _statistics(),
            GovernanceFeasibilityPolicy("permissive", None, None),
            _blocks(5.0, 10.0)[:3],
            _config(),
        )
    with pytest.raises(ValueError, match="latency block"):
        PilotLatencyBlock(0, 0.0, 1.0)
