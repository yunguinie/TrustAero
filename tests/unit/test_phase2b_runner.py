"""Tests for Phase 2B multi-candidate and lineage-aware measurements."""

from __future__ import annotations

import csv
import json

import pytest

from trustaero.experiments.phase2a import Phase2AConfig, run_phase2a
from trustaero.experiments.physical_sql import supported_phase2_materialization_targets
from trustaero.experiments.synthetic import SyntheticDataConfig


def test_phase2b_measures_and_deduplicates_approved_candidates() -> None:
    """Five legal candidates share results and price real source evidence."""

    pytest.importorskip("duckdb")
    output_dir = run_phase2a(
        Phase2AConfig(
            results_dir="results/test_phase2b_unit",
            workloads=(
                SyntheticDataConfig(
                    workload_id="phase2b-unit",
                    row_count=1000,
                    temporal_selectivity=0.6,
                    spatial_selectivity=0.5,
                    policy_selectivity=0.4,
                    join_match_rate=0.8,
                    hot_key_fraction=0.1,
                    seed=5,
                ),
            ),
            warmup_runs=0,
            measured_runs=1,
            materialization_targets=supported_phase2_materialization_targets(),
            source_lineage=True,
        )
    )

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["strategy_count"] == 5
    assert summary["source_lineage_enabled"] is True
    assert summary["all_results_equivalent"] is True
    workload = summary["workloads"][0]
    assert 1 <= workload["unique_physical_plan_count"] <= 5
    assert workload["deduplicated_candidate_count"] == (5 - workload["unique_physical_plan_count"])

    with (output_dir / "cases.csv").open(newline="", encoding="utf-8") as handle:
        rows = tuple(csv.DictReader(handle))
    assert len(rows) == 5
    assert all(row["lineage_level"] == "source" for row in rows)
    assert all(int(row["lineage_source_count"]) == 2 for row in rows)
    assert all(float(row["median_lineage_latency_ms"]) > 0 for row in rows)
    assert all(float(row["median_governed_latency_ms"]) > 0 for row in rows)
    winner = next(row for row in rows if row["strategy_id"] == workload["observed_median_winner"])
    assert winner["is_fingerprint_representative"] == "True"
