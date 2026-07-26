"""Tests for the frozen formal record-lineage evidence gates."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.record_lineage_evaluation import (
    RecordLineageEvaluationConfig,
    evaluate_record_lineage_unit,
    load_record_lineage_evaluation_config,
)
from trustaero.experiments.record_lineage_pilot import DIRECT, RECORD

ROOT = Path(__file__).resolve().parents[2]


def _config() -> RecordLineageEvaluationConfig:
    return RecordLineageEvaluationConfig(
        results_dir="results/evaluation",
        measurement_results_dir="results/measurement",
        measurement_config_path="experiments/configs/measurement.json",
        expected_row_counts=(100_000,),
        expected_blocks_per_unit=30,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=31,
        maximum_position_median_spread_fraction=0.3,
        maximum_half_ratio_drift_fraction=0.2,
        maximum_bytes_per_edge=65.0,
        expected_artifact_encoding="duckdb_digest_v3",
    )


def _rows(*, tamper_result: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for block in range(30):
        order = (DIRECT, RECORD) if block % 2 == 0 else (RECORD, DIRECT)
        for position, variant in enumerate(order):
            is_record = variant == RECORD
            rows.append(
                {
                    "row_count": 100_000,
                    "block_index": block,
                    "order_position": position,
                    "order_id": "->".join(order),
                    "variant": variant,
                    "result_digest": (
                        "tampered" if tamper_result and block == 0 and is_record else "same-result"
                    ),
                    "total_latency_ms": 20.0 if is_record else 10.0,
                    "lineage_capture_latency_ms": 19.0 if is_record else 0.0,
                    "lineage_verification_latency_ms": 8.0 if is_record else 0.0,
                    "lineage_edge_count": 100_000 if is_record else 0,
                    # Variable-length execution IDs may change only the compact
                    # artifact header, while the 64-byte edge payload is fixed.
                    "lineage_artifact_bytes": (6_400_400 + (block >= 10) if is_record else 0),
                    "lineage_edge_digest": "same-edges" if is_record else "",
                    "lineage_verified": is_record,
                }
            )
    return rows


def test_stable_formal_record_lineage_unit_passes() -> None:
    finding = evaluate_record_lineage_unit(_rows(), _config())

    assert finding["passed"]
    assert finding["paired_block_count"] == 30
    assert finding["median_record_over_direct_ratio"] == 2.0
    assert finding["bytes_per_edge"] == 64.00401
    assert finding["artifact_size_range_bytes"] == 1


def test_formal_record_lineage_result_mismatch_fails_closed() -> None:
    finding = evaluate_record_lineage_unit(
        _rows(tamper_result=True),
        _config(),
    )

    assert not finding["passed"]
    assert not finding["gates"]["one_result_digest"]


def test_ordinal_v4_evaluation_uses_the_32_byte_storage_gate() -> None:
    config = load_record_lineage_evaluation_config(
        ROOT / "experiments/configs/record_lineage_ordinal_formal_evaluation_v4.json"
    )

    assert config.expected_artifact_encoding == "ordinal_bound_v4"
    assert config.maximum_bytes_per_edge == 33.0
