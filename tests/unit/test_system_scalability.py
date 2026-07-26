"""Tests for the paired system-scalability experiment boundary."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter
from pathlib import Path

import pytest

from trustaero.experiments.system_scalability import (
    LAYER_IDS,
    ScalabilityWorkload,
    balanced_layer_orders,
    complete_permutation_layer_orders,
    load_system_scalability_config,
    semantic_result_digest,
    summarize_scalability_measurements,
    system_scalability_units,
)

ROOT = Path(__file__).resolve().parents[2]


def test_pilot_matrix_is_small_and_explicit() -> None:
    """The first run is a two-unit integrity pilot, not a paper-scale run."""

    config = load_system_scalability_config(
        ROOT / "experiments/configs/system_scalability_pilot_v1.json"
    )
    units = system_scalability_units(config)

    assert config.experiment_role == "system_scalability_governance_pilot"
    assert [unit.unit_id for unit in units] == ["bts-100000", "nyc_tlc-100000"]
    assert config.measured_blocks == 3


def test_500k_pilot_is_admitted_without_becoming_formal() -> None:
    config = load_system_scalability_config(
        ROOT / "experiments/configs/system_scalability_500k_pilot_v1.json"
    )
    units = system_scalability_units(config)

    assert [unit.unit_id for unit in units] == ["bts-500000", "nyc_tlc-500000"]
    assert config.experiment_role == "system_scalability_governance_pilot"
    assert config.measured_blocks == 3


def test_full_month_pilot_binds_both_real_workloads() -> None:
    """The scale pilot uses immutable January facts, not another sample."""

    config = load_system_scalability_config(
        ROOT / "experiments/configs/system_scalability_full_month_pilot_v1.json"
    )
    units = system_scalability_units(config)

    assert [unit.unit_id for unit in units] == [
        "bts-full_month",
        "nyc_tlc-full_month",
    ]
    assert config.experiment_role == "system_scalability_governance_pilot"
    assert config.measured_blocks == 3


def test_full_month_formal_uses_complete_permutation_cycles() -> None:
    config = load_system_scalability_config(
        ROOT / "experiments/configs/system_scalability_full_month_formal_v1.json"
    )

    assert config.experiment_role == "system_scalability_governance_formal"
    assert config.measured_blocks == 48
    assert config.ordering_design == "complete_permutation_cycles"
    assert [unit.unit_id for unit in system_scalability_units(config)] == [
        "bts-full_month",
        "nyc_tlc-full_month",
    ]


def test_pilot_protocol_binds_inputs_and_admits_no_paper_claim() -> None:
    """The source-lineage pilot cannot silently drift into a formal result."""

    protocol = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_source_lineage_pilot_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["scientific_role"].startswith("integrity pilot")
    assert any(
        "does not claim record-level lineage" in boundary
        for boundary in protocol["lineage_boundary"]
    )
    bindings = [
        protocol["measurement_config"],
        protocol["semantic_bindings"]["catalog"],
        protocol["semantic_bindings"]["policy"],
        *protocol["semantic_bindings"]["plans"],
    ]
    for binding in bindings:
        actual = hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest()
        assert actual == binding["sha256"]


def test_formal_protocol_binds_configs_and_positive_admissions() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_formal_source_lineage_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )
    bindings = [
        protocol["measurement_config"],
        protocol["evaluation_config"],
        *protocol["admission_results"],
    ]

    assert protocol["matrix"]["formal_measurement_count"] == 480
    for binding in bindings:
        actual = hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest()
        assert actual == binding["sha256"]


def test_bts100_confirmation_protocol_binds_configs_and_retains_failure() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_bts100_confirmation_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )
    retained = json.loads(
        (
            ROOT / "experiments/frozen/system_scalability_formal_partial_negative_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert retained["status"] == "RETAINED_PARTIAL_FAILURE"
    assert retained["inconclusive_units"] == ["bts-100000"]
    assert protocol["design"]["ordering"].startswith("two complete cycles")
    for binding in (protocol["measurement_config"], protocol["evaluation_config"]):
        actual = hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest()
        assert actual == binding["sha256"]


def test_bts100_confirmation_v2_binds_interleaved_schedule() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_bts100_confirmation_protocol_v2_20260724.json"
        ).read_text(encoding="utf-8")
    )
    negative = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_bts100_confirmation_v1_negative_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert negative["status"] == "RETAINED_CONFIRMATION_PROTOCOL_FAILURE"
    assert protocol["decision_policy"][1].startswith("A FAIL ends")
    assert protocol["design"]["temporal_interleaving"].startswith("no layer")
    for binding in (protocol["measurement_config"], protocol["evaluation_config"]):
        actual = hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest()
        assert actual == binding["sha256"]


def test_full_month_pilot_protocol_binds_config_and_manifest() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_full_month_pilot_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["timing"]["measured_query_count"] == 24
    assert protocol["decision_policy"][1].startswith("This three-block pilot")
    for binding in (
        protocol["predecessor_evidence"],
        protocol["measurement_config"],
        protocol["data_manifest"],
    ):
        actual = hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest()
        assert actual == binding["sha256"]


def test_full_month_formal_protocol_binds_admission_and_configs() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/"
            "system_scalability_full_month_formal_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["matrix"]["formal_measurement_count"] == 384
    assert protocol["ordering"]["maximum_identical_position_run"] == 3
    for binding in (
        protocol["pilot_admission"],
        protocol["measurement_config"],
        protocol["evaluation_config"],
    ):
        actual = hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest()
        assert actual == binding["sha256"]


def test_layer_schedule_is_deterministic_and_nearly_balanced() -> None:
    """Thirty blocks attain the mathematically possible +/-1 position balance."""

    first = balanced_layer_orders(30, seed=17)
    second = balanced_layer_orders(30, seed=17)

    assert first == second
    assert all(set(order) == set(LAYER_IDS) for order in first)
    for layer in LAYER_IDS:
        counts = Counter(
            position
            for order in first
            for position, candidate in enumerate(order)
            if candidate == layer
        )
        assert max(counts.values()) - min(counts.values()) <= 1


def test_complete_permutation_schedule_balances_positions_and_orders() -> None:
    """Confirmation runs cover every order equally, not merely every position."""

    orders = complete_permutation_layer_orders(48, seed=29)

    assert len(set(orders)) == 24
    assert all(orders.count(order) == 2 for order in set(orders))
    for layer in LAYER_IDS:
        positions = Counter(
            position
            for order in orders
            for position, candidate in enumerate(order)
            if candidate == layer
        )
        assert set(positions.values()) == {12}

        position_sequence = [
            position
            for order in orders
            for position, candidate in enumerate(order)
            if candidate == layer
        ]
        longest_run = max(len(list(group)) for _, group in itertools.groupby(position_sequence))
        assert longest_run <= 3


def test_complete_permutation_schedule_requires_whole_cycles() -> None:
    with pytest.raises(ValueError, match="multiple of 24"):
        complete_permutation_layer_orders(30, seed=29)


def test_invalid_scale_fails_closed() -> None:
    with pytest.raises(ValueError, match="positive"):
        ScalabilityWorkload("bts", (0,))


def test_semantic_digest_ignores_unspecified_row_order_but_keeps_duplicates() -> None:
    columns = ("borough", "trip_count")
    first = (("Queens", 3), ("Bronx", 2), ("Queens", 3))
    reordered = (("Queens", 3), ("Queens", 3), ("Bronx", 2))
    missing_duplicate = (("Queens", 3), ("Bronx", 2))

    assert semantic_result_digest(columns, first) == semantic_result_digest(columns, reordered)
    assert semantic_result_digest(columns, first) != semantic_result_digest(
        columns, missing_duplicate
    )


def test_summary_does_not_pool_layers() -> None:
    rows = []
    for layer_index, layer_id in enumerate(LAYER_IDS, start=1):
        for repeat in range(3):
            control_path_enabled = layer_id != "direct_database_equivalent_sql"
            lineage_enabled = layer_id in {
                "trustaero_with_source_lineage",
                "complete_trustaero_with_certificate",
            }
            certificate_enabled = layer_id == "complete_trustaero_with_certificate"
            rows.append(
                {
                    "unit_id": "bts-100000",
                    "layer_id": layer_id,
                    "end_to_end_latency_ms": float(layer_index + repeat),
                    "policy_validation_latency_ms": (0.1 if control_path_enabled else None),
                    "planner_latency_ms": 0.2 if control_path_enabled else None,
                    "database_execution_latency_ms": 1.0,
                    "lineage_capture_latency_ms": (0.3 if lineage_enabled else None),
                    "certificate_verification_latency_ms": (0.4 if certificate_enabled else None),
                    "output_rows": 10,
                }
            )

    summary = summarize_scalability_measurements(rows)

    assert summary["status"] == "PASS_SYSTEM_SCALABILITY_MEASUREMENT_INTEGRITY"
    assert summary["measurement_count"] == 12
    assert len(summary["layer_summaries"]) == 4
    direct = next(
        item
        for item in summary["layer_summaries"]
        if item["layer_id"] == "direct_database_equivalent_sql"
    )
    assert direct["median_policy_validation_latency_ms"] is None
