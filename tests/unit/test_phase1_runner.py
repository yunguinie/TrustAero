"""Tests for the Phase 1 DuckDB execution experiment runner."""

from __future__ import annotations

import csv
import json

import pytest

from trustaero.experiments.models import Phase1Config
from trustaero.experiments.phase1 import run_phase1


def test_run_phase1_writes_repeatable_execution_artifacts() -> None:
    """Phase 1 should produce paper-style artifacts for the DuckDB smoke path."""

    pytest.importorskip("duckdb")

    output_dir = run_phase1(
        Phase1Config(
            results_dir="results/test_phase1_unit",
            warmup_runs=0,
            measured_runs=1,
        )
    )

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["all_correct"] is True
    assert summary["case_count"] == 11
    assert summary["case_ids"] == [f"P1-{index:03d}" for index in range(1, 12)]
    assert summary["total_row_count"] == 19
    assert summary["result_correct"] == 11
    assert summary["source_lineage_cases"] == 1
    assert summary["unverified_components"] == ["physical_plan_execution"]
    with (output_dir / "cases.csv").open(newline="", encoding="utf-8") as handle:
        rows = tuple(csv.DictReader(handle))
    assert [row["status"] for row in rows] == ["PASS"] * 11
    assert [row["result_correct"] for row in rows] == ["True"] * 11
    lineage_row = next(row for row in rows if row["case_id"] == "P1-011")
    assert lineage_row["lineage_level"] == "source"
    assert lineage_row["lineage_source_count"] == "1"
    assert lineage_row["verified_obligation_count"] == "1"
    assert (output_dir / "environment.json").exists()
    assert (output_dir / "config.json").exists()
