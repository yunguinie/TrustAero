"""Predeclared integrity gates for a completed real-data infrastructure pilot."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

PILOT_LABEL = "real_data_infrastructure_pilot_not_paper_performance_evidence"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def analyze_real_data_pilot(
    run_dir: Path,
    *,
    max_scale_selectivity_drift: float = 0.02,
) -> dict[str, Any]:
    """Check completeness and semantic integrity without judging optimizer speedup."""

    if not 0.0 <= max_scale_selectivity_drift <= 1.0:
        raise ValueError("max_scale_selectivity_drift must be in [0, 1]")
    summary = _load_object(run_dir / "summary.json")
    units = summary.get("units")
    if not isinstance(units, list):
        raise ValueError("summary units must be a list")
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        measurements = list(csv.DictReader(handle))

    unit_gates: dict[str, dict[str, bool]] = {}
    selectivities: dict[str, list[float]] = defaultdict(list)
    expected_measurements = 0
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("summary unit must be an object")
        unit_id = str(unit["unit_id"])
        workload = str(unit["workload"])
        sample_rows = int(unit["sample_rows"])
        stage = unit["stage_statistics"]
        latency = unit["latency_summary"]
        physical = unit["physical_plan"]
        verified = set(unit["verified_obligations"])
        required = {"VERSION_PIN", "LINEAGE_CAPTURE"}
        if workload == "bts":
            required.add("MASK")
        expected_measurements += int(latency["measured_runs"])
        selectivities[workload].append(float(stage["governed_selectivity"]))
        unit_gates[unit_id] = {
            "status_pass": unit["status"] == "PASS",
            "input_matches_sample": int(stage["input_rows"]) == sample_rows,
            "cardinality_chain_valid": (
                0
                < int(stage["governed_rows"])
                <= int(stage["temporal_rows"])
                <= int(stage["input_rows"])
            ),
            "certificate_boundary_preserved": unit["certificate_status"] == "PARTIAL",
            "required_obligations_verified": required.issubset(verified),
            "no_raw_sensitive_exposure": int(unit["raw_sensitive_exposure_rows"]) == 0,
            "lineage_sources_correct": int(unit["lineage_source_count"])
            == (1 if workload == "bts" else 2),
            "physical_plan_observed": (
                str(physical["fingerprint"]).startswith("sha256:")
                and bool(physical["operator_names"])
            ),
            "latency_samples_valid": (
                int(latency["measured_runs"]) >= 1
                and 0.0
                < float(latency["min_ms"])
                <= float(latency["median_ms"])
                <= float(latency["p95_ms"])
                <= float(latency["max_ms"])
            ),
            "optimizer_claim_disabled": (
                int(unit["candidate_count"]) == 1
                and unit["optimizer_comparison_permitted"] is False
            ),
        }

    selectivity_drift = {
        workload: max(values) - min(values) if values else 0.0
        for workload, values in sorted(selectivities.items())
    }
    global_gates = {
        "summary_pass": summary.get("status") == "PASS",
        "all_units_complete": int(summary.get("completed_units", -1))
        == int(summary.get("expected_units", -2))
        == len(units),
        "scientific_boundary_preserved": (
            summary.get("scientific_label") == PILOT_LABEL
            and summary.get("paper_performance_evidence") is False
            and summary.get("optimizer_comparison_permitted") is False
        ),
        "measurement_count_complete": len(measurements) == expected_measurements,
        "scale_selectivity_stable": all(
            drift <= max_scale_selectivity_drift for drift in selectivity_drift.values()
        ),
    }
    all_passed = all(global_gates.values()) and all(
        value for gates in unit_gates.values() for value in gates.values()
    )
    payload = {
        "schema_version": 1,
        "run_id": summary.get("run_id", run_dir.name),
        "status": "PASS" if all_passed else "FAIL",
        "scientific_label": PILOT_LABEL,
        "paper_performance_evidence": False,
        "max_scale_selectivity_drift": max_scale_selectivity_drift,
        "observed_selectivity_drift": selectivity_drift,
        "global_gates": global_gates,
        "unit_gates": unit_gates,
    }
    _write_json(run_dir / "acceptance.json", payload)
    _write_report(run_dir / "report.md", summary, payload)
    return payload


def _write_report(path: Path, summary: dict[str, Any], acceptance: dict[str, Any]) -> None:
    lines = [
        "# Real-data infrastructure pilot",
        "",
        f"Status: **{acceptance['status']}**",
        "",
        "This is an infrastructure pilot, not paper performance evidence and not an "
        "optimizer comparison.",
        "",
        "| Unit | Input | Governed | Selectivity | Median (ms) | P95 (ms) | Spill (MiB) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for unit in summary["units"]:
        stage = unit["stage_statistics"]
        latency = unit["latency_summary"]
        physical = unit["physical_plan"]
        lines.append(
            "| {unit} | {input_rows} | {governed_rows} | {selectivity:.4f} | "
            "{median:.3f} | {p95:.3f} | {spill:.2f} |".format(
                unit=unit["unit_id"],
                input_rows=stage["input_rows"],
                governed_rows=stage["governed_rows"],
                selectivity=stage["governed_selectivity"],
                median=latency["median_ms"],
                p95=latency["p95_ms"],
                spill=physical["peak_temp_directory_bytes"] / (1024 * 1024),
            )
        )
    lines.extend(
        [
            "",
            "## Integrity gates",
            "",
            *[
                f"- {'PASS' if passed else 'FAIL'}: `{name}`"
                for name, passed in acceptance["global_gates"].items()
            ],
            "",
            "The timings include DuckDB execution, client result materialization, and the "
            "executor's result digest. They must not be compared with a server-only latency "
            "reported by another system.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
