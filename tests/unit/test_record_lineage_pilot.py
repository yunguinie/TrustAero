"""Tests for the bounded record-lineage pilot protocol."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from trustaero.experiments.record_lineage_pilot import (
    DIRECT,
    RECORD,
    load_record_lineage_pilot_config,
    record_lineage_orders,
)

ROOT = Path(__file__).resolve().parents[2]


def test_record_lineage_pilot_matrix_is_bounded() -> None:
    config = load_record_lineage_pilot_config(
        ROOT / "experiments/configs/record_lineage_pilot_v1.json"
    )

    assert config.row_counts == (1_000, 10_000, 50_000)
    assert config.blocks_per_unit == 4
    assert config.experiment_role == "record_lineage_integrity_pilot"
    assert config.artifact_encoding == "object_json_v1"


def test_compact_record_lineage_pilot_changes_only_encoding() -> None:
    config = load_record_lineage_pilot_config(
        ROOT / "experiments/configs/record_lineage_compact_pilot_v2.json"
    )

    assert config.row_counts == (1_000, 10_000, 50_000)
    assert config.artifact_encoding == "compact_binary_v2"


def test_database_digest_pilot_keeps_the_bounded_v2_matrix() -> None:
    config = load_record_lineage_pilot_config(
        ROOT / "experiments/configs/record_lineage_database_pilot_v3.json"
    )

    assert config.row_counts == (1_000, 10_000, 50_000)
    assert config.blocks_per_unit == 4
    assert config.artifact_encoding == "duckdb_digest_v3"


def test_formal_database_digest_matrix_has_thirty_paired_blocks() -> None:
    config = load_record_lineage_pilot_config(
        ROOT / "experiments/configs/record_lineage_formal_v3.json"
    )

    assert config.row_counts == (100_000, 500_000)
    assert config.blocks_per_unit == 30
    assert config.experiment_role == "record_lineage_scalability_formal"
    assert config.artifact_encoding == "duckdb_digest_v3"


def test_ordinal_v4_pilot_changes_encoding_not_the_development_matrix() -> None:
    config = load_record_lineage_pilot_config(
        ROOT / "experiments/configs/record_lineage_ordinal_pilot_v4.json"
    )

    assert config.row_counts == (1_000, 10_000, 50_000)
    assert config.blocks_per_unit == 4
    assert config.artifact_encoding == "ordinal_bound_v4"


def test_ordinal_v4_formal_matrix_has_thirty_paired_blocks() -> None:
    config = load_record_lineage_pilot_config(
        ROOT / "experiments/configs/record_lineage_ordinal_formal_v4.json"
    )

    assert config.row_counts == (100_000, 500_000)
    assert config.blocks_per_unit == 30
    assert config.experiment_role == "record_lineage_scalability_formal"
    assert config.artifact_encoding == "ordinal_bound_v4"


def test_record_lineage_orders_are_paired_and_position_balanced() -> None:
    orders = record_lineage_orders(3, seed=17)

    assert len(orders) == 6
    assert all(set(order) == {DIRECT, RECORD} for order in orders)
    for variant in (DIRECT, RECORD):
        positions = Counter(
            position
            for order in orders
            for position, candidate in enumerate(order)
            if candidate == variant
        )
        assert positions == {0: 3, 1: 3}


def test_record_lineage_pilot_protocol_binds_config_and_boundary() -> None:
    protocol = json.loads(
        (ROOT / "experiments/frozen/record_lineage_v1_pilot_protocol_20260724.json").read_text(
            encoding="utf-8"
        )
    )
    binding = protocol["measurement_config"]

    assert "Aggregate" in protocol["unsupported_fail_closed"]
    assert protocol["decision_policy"][0].startswith("This pilot cannot")
    assert hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]


def test_compact_record_lineage_protocol_binds_v1_and_v2_config() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/record_lineage_compact_v2_pilot_protocol_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["encoding_change"]["expected_edge_payload_bytes"] == 64
    for binding in (protocol["v1_baseline"], protocol["measurement_config"]):
        assert (
            hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
        )


def test_database_digest_protocol_binds_v2_result_and_v3_config() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/record_lineage_database_v3_pilot_protocol_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["scope"]["key_type"] == "STRING"
    assert protocol["decision_policy"][0].startswith("V3 remains")
    for binding in (protocol["v2_positive_record"], protocol["measurement_config"]):
        assert (
            hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
        )


def test_formal_record_lineage_protocol_binds_all_frozen_inputs() -> None:
    protocol = json.loads(
        (ROOT / "experiments/frozen/record_lineage_formal_v3_protocol_20260724.json").read_text(
            encoding="utf-8"
        )
    )

    assert protocol["measurement_design"]["paired_blocks_per_scale"] == 30
    assert protocol["predeclared_gates"]["maximum_bytes_per_edge"] == 65.0
    for key in ("admission_record", "measurement_config", "evaluation_config"):
        binding = protocol[key]
        assert (
            hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
        )


def test_ordinal_v4_protocol_binds_formal_v3_baseline_and_pilot() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/record_lineage_ordinal_v4_pilot_protocol_20260725.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["encoding"]["source_identity_bytes_per_edge"] == 32
    assert "row_reordering" in protocol["required_attack_tests"]
    for binding in (protocol["formal_v3_baseline"], protocol["measurement_config"]):
        assert (
            hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
        )


def test_ordinal_v4_formal_protocol_binds_all_inputs() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/record_lineage_ordinal_v4_formal_protocol_20260725.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["predeclared_gates"]["maximum_bytes_per_edge"] == 33.0
    for key in ("pilot_admission", "measurement_config", "evaluation_config"):
        binding = protocol[key]
        assert (
            hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
        )
