"""Tests for Phase 2A controlled physical-plan artifacts."""

from __future__ import annotations

import csv
import json

import pytest

from trustaero.experiments.phase2a import Phase2AConfig, run_phase2a
from trustaero.experiments.synthetic import SyntheticDataConfig


def test_phase2a_requires_result_equivalence_and_real_plan_difference() -> None:
    """The runner must inspect DuckDB plans instead of trusting strategy names."""

    pytest.importorskip("duckdb")
    output_dir = run_phase2a(
        Phase2AConfig(
            results_dir="results/test_phase2a_unit",
            workloads=(
                SyntheticDataConfig(
                    workload_id="unit",
                    row_count=1000,
                    temporal_selectivity=0.60,
                    spatial_selectivity=0.60,
                    policy_selectivity=0.60,
                    join_match_rate=0.80,
                    hot_key_fraction=0.10,
                    seed=3,
                ),
            ),
            warmup_runs=0,
            measured_runs=1,
        )
    )

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["all_results_equivalent"] is True
    assert summary["all_physical_plans_distinct"] is True
    assert summary["all_candidates_approved"] is True
    assert len(summary["controlled_statistics"]) == 6
    with (output_dir / "cases.csv").open(newline="", encoding="utf-8") as handle:
        rows = tuple(csv.DictReader(handle))
    assert len(rows) == 2
    assert {row["strategy"] for row in rows} == {"fused", "materialized_cte"}
    assert len({row["physical_plan_fingerprint"] for row in rows}) == 2
    assert all(row["result_equivalent"] == "True" for row in rows)
    # Both measurements must share logical semantics but use independently
    # auditable physical plan identities.
    assert len({row["logical_plan_id"] for row in rows}) == 1
    assert len({row["approved_physical_plan_id"] for row in rows}) == 2
    assert all(row["strategy_id"] for row in rows)
    assert len(tuple((output_dir / "plans").glob("*.json"))) == 2
