"""Frozen-gate evaluation for Phase 0 fault injection."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from trustaero.experiments.reporting import summarize_run
from trustaero.reproducibility.source_freeze import sha256_file


def evaluate_phase0_run(
    run_dir: Path,
    protocol_path: Path,
) -> dict[str, Any]:
    """Evaluate correctness gates while reporting overhead without tuning it."""

    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    with (run_dir / "cases.csv").open(newline="", encoding="utf-8") as handle:
        rows = tuple(csv.DictReader(handle))
    summary = summarize_run(run_dir)
    planner_rows = [row for row in rows if row["scenario"].startswith("planner_")]
    certificate_rows = [row for row in rows if row["case_kind"] == "certificate"]
    gates_config = protocol["gates"]
    gates = {
        "expected_case_count": len(rows) == int(protocol["expected_case_count"]),
        "expected_planner_binding_case_count": (
            len(planner_rows) == int(protocol["expected_planner_binding_case_count"])
        ),
        "minimum_status_accuracy": (
            summary.status_accuracy >= float(gates_config["minimum_status_accuracy"])
        ),
        "minimum_reason_code_accuracy": (
            summary.reason_code_accuracy >= float(gates_config["minimum_reason_code_accuracy"])
        ),
        "minimum_detection_rate": (
            summary.detection_rate >= float(gates_config["minimum_detection_rate"])
        ),
        "maximum_false_reject_rate": (
            summary.false_reject_rate <= float(gates_config["maximum_false_reject_rate"])
        ),
        "planner_latency_observed": (
            not gates_config["require_planner_latency_observations"]
            or all(float(row["planner_median_latency_ms"]) > 0.0 for row in planner_rows)
        ),
        "certificate_latency_observed": (
            not gates_config["require_certificate_latency_observations"]
            or all(
                float(row["certificate_verification_median_latency_ms"]) > 0.0
                for row in certificate_rows
            )
        ),
    }
    passed = all(gates.values())
    planner_latencies = [float(row["planner_median_latency_ms"]) for row in planner_rows]
    certificate_latencies = [
        float(row["certificate_verification_median_latency_ms"]) for row in certificate_rows
    ]
    return {
        "schema_version": 1,
        "status": (
            "PASS_PHASE0_PLANNER_FAULT_INJECTION"
            if passed
            else "FAIL_PHASE0_PLANNER_FAULT_INJECTION_RETAIN"
        ),
        "run_id": run_dir.name,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": sha256_file(protocol_path),
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "metrics": {
            "case_count": summary.case_count,
            "status_accuracy": summary.status_accuracy,
            "reason_code_accuracy": summary.reason_code_accuracy,
            "detection_rate": summary.detection_rate,
            "false_reject_rate": summary.false_reject_rate,
            "median_planner_latency_ms": (
                sum(planner_latencies) / len(planner_latencies) if planner_latencies else 0.0
            ),
            "median_certificate_verification_latency_ms": (
                sum(certificate_latencies) / len(certificate_latencies)
                if certificate_latencies
                else 0.0
            ),
            "p95_latency_ms": summary.p95_latency_ms,
            "max_latency_ms": summary.max_latency_ms,
        },
        "scientific_boundary": protocol["scientific_boundary"],
        "optimizer_speedup_claim_authorized": False,
    }
