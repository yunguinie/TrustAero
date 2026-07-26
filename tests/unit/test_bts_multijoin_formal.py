"""Unit tests for four-candidate BTS multi-Join protocol controls."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.bts_multijoin_formal import (
    load_bts_multijoin_formal_config,
)
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_multijoin_config_requires_complete_permutation_cycles() -> None:
    config = load_bts_multijoin_formal_config(
        PROJECT_ROOT / "experiments/configs/bts_multijoin_formal_v1.json"
    )

    with pytest.raises(ValueError, match="all 24 permutations"):
        replace(config, measured_blocks=50)
    with pytest.raises(ValueError, match="not optimizer holdout"):
        # The exact wording comes from the common scope guard.
        replace(config, heldout_optimizer_evidence=True)


def test_four_candidate_schedule_balances_all_permutations_and_positions() -> None:
    candidates = ("fused", "after-filter", "after-origin", "after-carrier")
    orders = complete_permutation_orders(candidates, 48, seed=17)

    assert len(set(orders)) == 24
    assert all(orders.count(order) == 2 for order in set(orders))
    for candidate in candidates:
        assert [order.index(candidate) for order in orders].count(0) == 12
        assert [order.index(candidate) for order in orders].count(3) == 12
