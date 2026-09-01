"""Tests for frozen Phase 0 correctness and overhead gates."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from trustaero.experiments.phase0_evaluation import evaluate_phase0_run


def _write_protocol(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "protocol_id": "test",
                "expected_case_count": 2,
                "expected_planner_binding_case_count": 1,
                "gates": {
                    "minimum_status_accuracy": 1.0,
                    "minimum_reason_code_accuracy": 1.0,
                    "minimum_detection_rate": 1.0,
                    "maximum_false_reject_rate": 0.0,
                    "require_planner_latency_observations": True,
                    "require_certificate_latency_observations": True,
                },
                "scientific_boundary": ["test-only"],
            }
        ),
        encoding="utf-8",
    )


def _write_cases(path: Path, *, detected: bool) -> None:
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
        "median_latency_ms",
        "p95_latency_ms",
        "max_latency_ms",
        "planner_median_latency_ms",
        "certificate_verification_median_latency_ms",
    ]
    rows = [
        {
            "run_id": "run",
            "commit_hash": "abc",
            "case_id": "legal",
            "case_category": "legal",
            "case_kind": "validation",
            "scenario": "baseline",
            "expected_status": "ACCEPT",
            "actual_status": "ACCEPT",
            "status_correct": "True",
            "expected_reason_codes": "",
            "actual_reason_codes": "",
            "reason_code_correct": "True",
            "median_latency_ms": "1",
            "p95_latency_ms": "2",
            "max_latency_ms": "3",
            "planner_median_latency_ms": "0",
            "certificate_verification_median_latency_ms": "0",
        },
        {
            "run_id": "run",
            "commit_hash": "abc",
            "case_id": "fault",
            "case_category": "planner",
            "case_kind": "certificate",
            "scenario": "planner_digest_mismatch",
            "expected_status": "REJECT",
            "actual_status": "REJECT" if detected else "PARTIAL",
            "status_correct": "True" if detected else "False",
            "expected_reason_codes": "CERTIFICATE_PLANNER_DECISION_MISMATCH",
            "actual_reason_codes": ("CERTIFICATE_PLANNER_DECISION_MISMATCH" if detected else ""),
            "reason_code_correct": "True" if detected else "False",
            "median_latency_ms": "1",
            "p95_latency_ms": "2",
            "max_latency_ms": "3",
            "planner_median_latency_ms": "0.1",
            "certificate_verification_median_latency_ms": "0.2",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_phase0_evaluation_passes_complete_detection(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    protocol = tmp_path / "protocol.json"
    _write_protocol(protocol)
    _write_cases(run / "cases.csv", detected=True)

    result = evaluate_phase0_run(run, protocol)

    assert result["status"] == "PASS_PHASE0_PLANNER_FAULT_INJECTION"
    assert result["failed_gates"] == []


def test_phase0_evaluation_retains_detection_failure(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    protocol = tmp_path / "protocol.json"
    _write_protocol(protocol)
    _write_cases(run / "cases.csv", detected=False)

    result = evaluate_phase0_run(run, protocol)

    assert result["status"] == "FAIL_PHASE0_PLANNER_FAULT_INJECTION_RETAIN"
    assert "minimum_detection_rate" in result["failed_gates"]
