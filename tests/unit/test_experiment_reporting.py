"""Tests for Phase 0 experiment reporting artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from trustaero.experiments.reporting import summarize_phase0


def _write_cases_csv(path: Path) -> None:
    """Create a tiny Phase 0 result file with one passing and one failing case."""

    fieldnames = [
        "run_id",
        "commit_hash",
        "case_id",
        "case_category",
        "case_kind",
        "scenario",
        "expected_status",
        "actual_status",
        "status_correct",
        "expected_reason_codes",
        "actual_reason_codes",
        "reason_code_correct",
        "runs",
        "warmup_runs",
        "cold_latency_ms",
        "median_latency_ms",
        "p95_latency_ms",
        "min_latency_ms",
        "max_latency_ms",
        "plan_size_bytes",
        "operator_count",
        "edge_count",
        "rewrite_rounds",
        "inserted_operator_count",
        "pending_obligation_count",
        "verified_obligation_count",
        "certificate_event_count",
        "plan_digest",
    ]
    rows = [
        {
            "run_id": "run-a",
            "commit_hash": "abc123",
            "case_id": "P0-001",
            "case_category": "basic_accept",
            "case_kind": "validation",
            "scenario": "baseline",
            "expected_status": "ACCEPT",
            "actual_status": "ACCEPT",
            "status_correct": "True",
            "expected_reason_codes": "",
            "actual_reason_codes": "",
            "reason_code_correct": "True",
            "runs": "2",
            "warmup_runs": "1",
            "cold_latency_ms": "1.0",
            "median_latency_ms": "0.5",
            "p95_latency_ms": "0.7",
            "min_latency_ms": "0.4",
            "max_latency_ms": "0.8",
            "plan_size_bytes": "100",
            "operator_count": "1",
            "edge_count": "0",
            "rewrite_rounds": "",
            "inserted_operator_count": "0",
            "pending_obligation_count": "0",
            "verified_obligation_count": "0",
            "certificate_event_count": "0",
            "plan_digest": "digest-a",
        },
        {
            "run_id": "run-a",
            "commit_hash": "abc123",
            "case_id": "P0-002",
            "case_category": "field_semantics",
            "case_kind": "validation",
            "scenario": "masked_filter",
            "expected_status": "REJECT",
            "actual_status": "REJECT",
            "status_correct": "True",
            "expected_reason_codes": "MASKED_FIELD_USED_SEMANTICALLY|FIELD_NOT_AVAILABLE",
            "actual_reason_codes": "MASKED_FIELD_USED_SEMANTICALLY",
            "reason_code_correct": "False",
            "runs": "2",
            "warmup_runs": "1",
            "cold_latency_ms": "2.0",
            "median_latency_ms": "1.5",
            "p95_latency_ms": "1.7",
            "min_latency_ms": "1.4",
            "max_latency_ms": "1.8",
            "plan_size_bytes": "200",
            "operator_count": "2",
            "edge_count": "1",
            "rewrite_rounds": "",
            "inserted_operator_count": "0",
            "pending_obligation_count": "0",
            "verified_obligation_count": "0",
            "certificate_event_count": "0",
            "plan_digest": "digest-b",
        },
    ]

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_summarize_phase0_writes_category_and_reason_code_tables(tmp_path: Path) -> None:
    """The paper-facing summary keeps grouped coverage separate from raw cases."""

    run_dir = tmp_path / "phase0" / "run-a"
    run_dir.mkdir(parents=True)
    _write_cases_csv(run_dir / "cases.csv")

    output_dir = tmp_path / "summary"
    summaries = summarize_phase0(tmp_path / "phase0", output_dir)

    assert summaries[0].case_count == 2
    assert summaries[0].all_correct is False

    with (output_dir / "phase0_category_summary.csv").open(newline="", encoding="utf-8") as handle:
        categories = {row["case_category"]: row for row in csv.DictReader(handle)}
    assert categories["basic_accept"]["case_count"] == "1"
    assert categories["field_semantics"]["failed_cases"] == "P0-002"

    reason_summary = json.loads(
        (output_dir / "phase0_reason_code_summary.json").read_text(encoding="utf-8")
    )
    by_code = {row["reason_code"]: row for row in reason_summary}
    assert by_code["MASKED_FIELD_USED_SEMANTICALLY"] == {
        "actual_count": 1,
        "expected_count": 1,
        "matched_count": 1,
        "reason_code": "MASKED_FIELD_USED_SEMANTICALLY",
        "run_id": "run-a",
    }
    assert by_code["FIELD_NOT_AVAILABLE"]["matched_count"] == 0
