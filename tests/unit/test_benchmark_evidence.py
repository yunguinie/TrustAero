"""Tests for the cross-workload evidence boundary and ratio calculations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.benchmark_evidence import (
    EvidenceSource,
    _candidate_rows,
    load_benchmark_evidence_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_evidence_config_binds_four_distinct_workloads() -> None:
    config = load_benchmark_evidence_config(
        PROJECT_ROOT / "experiments/configs/benchmark_evidence_matrix_v1.json"
    )

    assert len(config.sources) == 4
    assert {item.domain for item in config.sources} == {
        "standard_benchmark",
        "real_air_transport",
        "real_urban_mobility",
    }
    with pytest.raises(ValueError, match="inside the project"):
        replace(config.sources[0], summary_path="../outside.json")


def test_candidate_rows_compute_oracle_and_reference_regret() -> None:
    source = EvidenceSource(
        workload_id="fixture",
        domain="test",
        query_family="filter_mask",
        evidence_scope="development",
        reference_candidate_id="fused",
        accepted_oracle_set=("materialized",),
        summary_path="summary.json",
        summary_sha256="0" * 64,
        acceptance_path="acceptance.json",
        acceptance_sha256="1" * 64,
    )
    summary = {
        "candidate_summaries": {
            "fused": {
                "median_ms": 120.0,
                "p95_ms": 130.0,
                "peak_buffer_memory_bytes": 1048576,
                "peak_temp_directory_bytes": 0,
            },
            "materialized": {
                "median_ms": 100.0,
                "p95_ms": 110.0,
                "peak_buffer_memory_bytes": 2097152,
                "peak_temp_directory_bytes": 0,
            },
        }
    }

    rows, workload = _candidate_rows(source, summary, tie_threshold=0.03)

    assert workload["oracle_set_within_tie_band"] == ["materialized"]
    assert workload["pooled_median_reference_regret_percent"] == pytest.approx(20.0)
    assert workload["alternative_boundary_within_tie_band"] is True
    assert workload["reference_outside_oracle_set"] is True
    assert {row["candidate_id"] for row in rows} == {"fused", "materialized"}
