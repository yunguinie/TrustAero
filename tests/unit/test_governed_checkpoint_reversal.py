from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.governed_checkpoint_reversal import (
    EA1_CANDIDATE_IDS,
    GovernedCheckpointUnit,
    _candidate_sql,
    _create_data,
    _execute_candidate,
    _feasibility,
    checkpoint_orders,
    governed_checkpoint_units,
    load_governed_checkpoint_config,
)


def _config_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "experiments/configs/governed_checkpoint_reversal_v1.json"
    )


def test_frozen_matrix_and_orders_are_complete_and_balanced() -> None:
    config = load_governed_checkpoint_config(_config_path())
    orders = checkpoint_orders(
        config.candidate_ids,
        config.repetitions_per_permutation,
        seed=config.order_seed,
    )

    assert len(governed_checkpoint_units(config)) == 24
    assert len(orders) == 30
    assert set(orders) == {EA1_CANDIDATE_IDS, tuple(reversed(EA1_CANDIDATE_IDS))}
    assert all(orders.count(order) == 15 for order in set(orders))
    for candidate_id in EA1_CANDIDATE_IDS:
        assert sum(order[0] == candidate_id for order in orders) == 15
        assert sum(order[1] == candidate_id for order in orders) == 15


def test_config_rejects_candidate_cherry_picking_and_too_few_repeats() -> None:
    config = load_governed_checkpoint_config(_config_path())

    with pytest.raises(ValueError, match="both frozen"):
        replace(config, candidate_ids=(EA1_CANDIDATE_IDS[0],))
    with pytest.raises(ValueError, match="15 complete"):
        replace(config, repetitions_per_permutation=14)


def test_policy_gate_rejects_raw_checkpoint_before_cost() -> None:
    result = _feasibility({"query_rows": 25})

    assert result["permissive_feasible_candidate_ids"] == list(EA1_CANDIDATE_IDS)
    assert result["strict_feasible_candidate_ids"] == [EA1_CANDIDATE_IDS[0]]
    assert result["strict_rejected_candidate_ids"] == [EA1_CANDIDATE_IDS[1]]


def test_candidate_templates_are_distinct_and_result_equivalent() -> None:
    duckdb = pytest.importorskip("duckdb")
    unit = GovernedCheckpointUnit(1_000, 64, 0.2, 0.3, 17)
    connection = duckdb.connect(":memory:")
    try:
        actual = _create_data(connection, unit)
        rows = [
            _execute_candidate(
                connection,
                unit,
                candidate_id,
                repeat_index=0,
                order_position=position,
                permutation_id="test",
            )
            for position, candidate_id in enumerate(EA1_CANDIDATE_IDS)
        ]
        assert actual["result_rows"] > 0
        assert rows[0]["result_digest"] == rows[1]["result_digest"]
        assert _candidate_sql(EA1_CANDIDATE_IDS[0], unit) != _candidate_sql(
            EA1_CANDIDATE_IDS[1], unit
        )
    finally:
        connection.close()
