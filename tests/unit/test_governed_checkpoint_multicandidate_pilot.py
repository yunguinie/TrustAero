"""Tests for the frozen three-candidate label-diversity pilot."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.governed_checkpoint_multicandidate import (
    MULTICANDIDATE_IDS,
)
from trustaero.experiments.governed_checkpoint_multicandidate_pilot import (
    load_multicandidate_pilot_config,
    multicandidate_pilot_units,
)
from trustaero.experiments.governed_checkpoint_reversal import checkpoint_orders


def _config_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "experiments/configs/governed_checkpoint_multicandidate_pilot_v1.json"
    )


def test_frozen_matrix_and_orders_are_complete() -> None:
    config = load_multicandidate_pilot_config(_config_path())
    units = multicandidate_pilot_units(config)
    orders = checkpoint_orders(
        config.candidate_ids,
        config.measured_rounds_per_permutation,
        seed=config.order_seed,
    )

    assert len(units) == 36
    assert len({unit.scenario_id for unit in units}) == 12
    assert config.measured_blocks_per_unit == 30
    assert len(orders) == 30
    assert set(orders) == set(__import__("itertools").permutations(MULTICANDIDATE_IDS))
    assert all(orders.count(order) == 5 for order in set(orders))


def test_config_rejects_candidate_or_gate_cherry_picking() -> None:
    config = load_multicandidate_pilot_config(_config_path())

    with pytest.raises(ValueError, match="all three"):
        replace(config, candidate_ids=MULTICANDIDATE_IDS[:2])
    with pytest.raises(ValueError, match="five permutation"):
        replace(config, measured_rounds_per_permutation=4)
    with pytest.raises(ValueError, match="winner diversity"):
        replace(config, minimum_distinct_singleton_winners=1)
