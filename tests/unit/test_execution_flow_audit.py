from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.execution_flow_audit import (
    ExecutionFlowUnit,
    _balanced_carryover_orders,
    execution_flow_units,
    execution_flow_variants,
    load_execution_flow_audit_config,
    observed_operator_columns,
    physical_work_vector,
)
from trustaero.experiments.execution_flow_inference import (
    hierarchical_paired_log_ratio_ci,
)


def _config_path() -> Path:
    return (
        Path(__file__).resolve().parents[2] / "experiments/configs/execution_flow_audit_smoke.json"
    )


def test_matrix_retains_all_execution_mechanisms_and_equivalence_groups() -> None:
    variants = execution_flow_variants()

    assert len(variants) == 11
    assert {item.equivalence_group for item in variants} == {
        "column_pruning",
        "mask_output",
        "mask_aggregate",
        "mask_sorted_output",
    }
    assert {item.mask_placement for item in variants} == {
        "none",
        "before_join",
        "after_join",
    }
    assert {item.materialization_boundary for item in variants} == {
        "none",
        "optimizer_prunable",
        "raw_after_join",
        "masked_before_join",
    }
    assert {item.variant_id for item in variants if item.evaluation_role == "mechanism_only"} == {
        "join_key_only_aggregate",
        "dead_raw_projection_aggregate",
        "raw_materialized_aggregate",
    }
    assert sum(item.evaluation_role == "deployable" for item in variants) == 8


def test_config_rejects_cherry_picked_variants_and_expands_complete_units() -> None:
    config = load_execution_flow_audit_config(_config_path())

    assert len(execution_flow_units(config)) == 1
    with pytest.raises(ValueError, match="full EA-0 matrix"):
        replace(config, variant_ids=config.variant_ids[:-1])


def test_williams_orders_balance_positions_and_direct_predecessors() -> None:
    """The formal order must not leave one fixed predecessor per candidate."""

    candidates = tuple(f"candidate_{index}" for index in range(11))
    orders = _balanced_carryover_orders(candidates, 12, seed=17)
    symbols = set(orders[0])

    assert len(orders) == 12
    assert len(symbols) == 12  # Eleven candidates plus one neutral barrier.
    for position in range(12):
        assert {order[position] for order in orders} == symbols

    predecessor_pairs = [
        (order[position - 1], order[position])
        for order in orders
        for position in range(1, len(order))
    ]
    assert len(predecessor_pairs) == 12 * 11
    assert len(set(predecessor_pairs)) == 12 * 11


def test_carryover_design_rejects_incomplete_measurement_block() -> None:
    config = load_execution_flow_audit_config(_config_path())

    with pytest.raises(ValueError, match="augmented design size"):
        replace(config, order_design="balanced_carryover", measured_runs=11)


def test_physical_work_vectors_distinguish_candidate_specific_work() -> None:
    unit = ExecutionFlowUnit(
        row_count=1_000,
        identifier_width=256,
        match_rate=0.5,
        seed=17,
    )
    variants = {item.variant_id: item for item in execution_flow_variants()}
    early = physical_work_vector(unit, variants["prejoin_mask_materialized_output"])
    late = physical_work_vector(unit, variants["postjoin_mask_fused_output"])
    raw = physical_work_vector(unit, variants["postjoin_raw_materialized_mask_output"])
    key_only = physical_work_vector(unit, variants["join_key_only_aggregate"])

    assert early.estimated_mask_rows == 1_000
    assert late.estimated_mask_rows == 500
    assert early.estimated_masked_materialization_bytes == 80_000
    assert late.estimated_masked_materialization_bytes == 0
    assert raw.estimated_raw_materialization_bytes == 500 * (256 + 16)
    assert key_only.estimated_sensitive_scan_bytes == 0
    assert early.join_output_rows == late.join_output_rows == 500


def test_observed_columns_come_only_from_exposed_operator_metadata() -> None:
    plan = json.dumps(
        {
            "children": [
                {
                    "operator_name": "PROJECTION",
                    "extra_info": {"Projections": ["sha256(sensitive_value)"]},
                    "children": [
                        {
                            "operator_name": "SEQ_SCAN",
                            "extra_info": {"Projections": ["join_key", "row_id"]},
                            "children": [],
                        }
                    ],
                }
            ]
        }
    )

    assert observed_operator_columns(plan) == (
        ("sensitive_value",),
        ("row_id", "join_key"),
    )


def test_hierarchical_paired_ci_respects_seed_clusters() -> None:
    # Every seed independently says that the left candidate takes about twice
    # as long.  The interval should therefore remain far outside the 3% band.
    ratios = {
        11: [math.log(value) for value in (1.9, 2.0, 2.1, 2.0, 1.95)],
        22: [math.log(value) for value in (2.0, 2.05, 1.95, 2.1, 1.9)],
        33: [math.log(value) for value in (2.1, 2.0, 2.05, 1.95, 2.0)],
    }

    point, lower, upper = hierarchical_paired_log_ratio_ci(
        ratios,
        confidence_level=0.95,
        repetitions=1_000,
        seed=7,
    )

    assert point == pytest.approx(2.0)
    assert 1.8 < lower <= upper < 2.2
