"""Tests for Phase 1 reporting artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from trustaero.experiments.reporting import summarize_phase1


def _write_cases_csv(path: Path) -> None:
    """Create a tiny Phase 1 cases file with one pass and one fail."""

    fieldnames = [
        "run_id",
        "commit_hash",
        "case_id",
        "case_category",
        "scenario",
        "plan_id",
        "status",
        "status_correct",
        "row_count",
        "expected_row_count",
        "certificate_status",
        "result_digest",
        "unverified_components",
        "cold_latency_ms",
        "median_latency_ms",
        "p95_latency_ms",
        "min_latency_ms",
        "max_latency_ms",
        "sql_length",
        "parameter_count",
        "logical_plan_id",
        "physical_plan_id",
    ]
    rows = [
        {
            "run_id": "run-p1",
            "commit_hash": "abc123",
            "case_id": "P1-001",
            "case_category": "project",
            "scenario": "baseline_project",
            "plan_id": "p1",
            "status": "PASS",
            "status_correct": "True",
            "row_count": "2",
            "expected_row_count": "2",
            "certificate_status": "PARTIAL",
            "result_digest": "sha256:a",
            "unverified_components": "physical_plan_execution",
            "cold_latency_ms": "1.0",
            "median_latency_ms": "0.5",
            "p95_latency_ms": "0.7",
            "min_latency_ms": "0.4",
            "max_latency_ms": "0.8",
            "sql_length": "80",
            "parameter_count": "0",
            "logical_plan_id": "pl-a",
            "physical_plan_id": "pp-a",
        },
        {
            "run_id": "run-p1",
            "commit_hash": "abc123",
            "case_id": "P1-002",
            "case_category": "filter",
            "scenario": "magnitude_ge_5",
            "plan_id": "p2",
            "status": "FAIL",
            "status_correct": "False",
            "row_count": "0",
            "expected_row_count": "1",
            "certificate_status": "REJECT",
            "result_digest": "sha256:b",
            "unverified_components": "physical_plan_execution|result_content_digest",
            "cold_latency_ms": "2.0",
            "median_latency_ms": "1.5",
            "p95_latency_ms": "1.7",
            "min_latency_ms": "1.4",
            "max_latency_ms": "1.8",
            "sql_length": "120",
            "parameter_count": "1",
            "logical_plan_id": "pl-b",
            "physical_plan_id": "pp-b",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_summarize_phase1_writes_run_and_category_tables(tmp_path: Path) -> None:
    """Phase 1 reporting should summarize both runs and execution categories."""

    run_dir = tmp_path / "phase1" / "run-p1"
    run_dir.mkdir(parents=True)
    _write_cases_csv(run_dir / "cases.csv")

    output_dir = tmp_path / "summary"
    summaries = summarize_phase1(tmp_path / "phase1", output_dir)

    assert summaries[0].case_count == 2
    assert summaries[0].pass_count == 1
    assert summaries[0].all_correct is False
    assert summaries[0].failed_cases == ("P1-002",)

    summary_json = json.loads((output_dir / "phase1_summary.json").read_text(encoding="utf-8"))
    assert summary_json[0]["total_row_count"] == 2
    assert summary_json[0]["unverified_components"] == [
        "physical_plan_execution",
        "result_content_digest",
    ]

    with (output_dir / "phase1_category_summary.csv").open(newline="", encoding="utf-8") as handle:
        categories = {row["case_category"]: row for row in csv.DictReader(handle)}
    assert categories["project"]["pass_count"] == "1"
    assert categories["filter"]["failed_cases"] == "P1-002"
