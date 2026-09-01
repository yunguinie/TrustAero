"""Run the frozen persisted-manifest, cross-process Checker experiment."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def _object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _write(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_inputs(
    case_id: str,
    logical: JsonObject,
    physical: JsonObject,
    certificate: JsonObject,
    observation: JsonObject,
) -> tuple[JsonObject, JsonObject, JsonObject, JsonObject]:
    logical_case = copy.deepcopy(logical)
    physical_case = copy.deepcopy(physical)
    certificate_case = copy.deepcopy(certificate)
    observation_case = copy.deepcopy(observation)
    if case_id == "observed_result_tamper":
        observation_case["result_digest"] = "sha256:independent-observation-tampered"
    elif case_id == "certificate_result_tamper":
        certificate_case["result_digest"] = "sha256:certificate-result-tampered"
    elif case_id == "approval_manifest_tamper":
        physical_case["logical_plan_id"] = "pl-tampered-manifest"
    elif case_id != "untampered":
        raise ValueError(f"Unknown case: {case_id}")
    return logical_case, physical_case, certificate_case, observation_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("experiments/frozen/independent_checker_manifest_protocol_v1_20260812.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol_path = root / args.protocol
    protocol = _object(protocol_path)
    source = root / protocol["source_run"]
    for name, expected in protocol["source_files"].items():
        actual = _sha256(source / name)
        if actual != expected:
            raise ValueError(f"Frozen source digest mismatch for {name}: {actual}")

    logical = _object(source / "validated_plan.json")
    physical = _object(source / "approved_physical_plan.json")
    certificate = _object(source / "certificate.json")
    source_summary = _object(source / "summary.json")
    observation: JsonObject = {
        "result_digest": source_summary["execution"]["result_digest"],
        "planner_decision_digest": source_summary["candidate_planning"]["planner_decision_digest"],
        "planner_selected_candidate_id": source_summary["candidate_planning"][
            "selected_candidate_id"
        ],
    }

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = root / protocol["results_dir"] / run_id
    rows: list[JsonObject] = []
    for registered in protocol["cases"]:
        case_id = registered["case_id"]
        case_dir = out / "cases" / case_id
        approval_store = case_dir / "approval_store"
        execution_store = case_dir / "execution_evidence"
        checker_store = case_dir / "checker_output"
        logical_case, physical_case, certificate_case, observation_case = _case_inputs(
            case_id, logical, physical, certificate, observation
        )
        logical_path = approval_store / "validated_plan.json"
        physical_path = approval_store / "approved_physical_plan.json"
        certificate_path = execution_store / "certificate.json"
        observation_path = execution_store / "independent_observation.json"
        output_path = checker_store / "verification.json"
        _write(logical_path, logical_case)
        _write(physical_path, physical_case)
        _write(certificate_path, certificate_case)
        _write(observation_path, observation_case)
        checker_store.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "scripts.verify_certificate_bundle",
            "--logical-plan",
            str(logical_path),
            "--physical-plan",
            str(physical_path),
            "--certificate",
            str(certificate_path),
            "--observation",
            str(observation_path),
            "--output",
            str(output_path),
        ]
        environment = os.environ.copy()
        source_path = str(root / "src")
        environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
        completed = subprocess.run(
            command, cwd=root, env=environment, check=False, text=True, capture_output=True
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Checker process failed for {case_id}: {completed.stderr}")
        verification = _object(output_path)
        passed = verification["status"] == registered["expected_status"]
        rows.append(
            {
                "case_id": case_id,
                "expected_status": registered["expected_status"],
                "actual_status": verification["status"],
                "diagnostic_codes": [item["code"] for item in verification["diagnostics"]],
                "unverified_components": verification["unverified_components"],
                "fresh_process": True,
                "passed": passed,
                "input_sha256": {
                    "logical_plan": _sha256(logical_path),
                    "physical_plan": _sha256(physical_path),
                    "certificate": _sha256(certificate_path),
                    "observation": _sha256(observation_path),
                },
            }
        )

    untampered = next(row for row in rows if row["case_id"] == "untampered")
    gates = {
        "all_cases_in_fresh_process": all(row["fresh_process"] for row in rows),
        "all_expected_statuses": all(row["passed"] for row in rows),
        "untampered_is_partial": untampered["actual_status"] == "PARTIAL",
        "only_physical_execution_unverified": untampered["unverified_components"]
        == ["physical_plan_execution"],
        "all_tampered_rejected": all(
            row["actual_status"] == "REJECT" for row in rows if row["case_id"] != "untampered"
        ),
    }
    status = "PASS_INDEPENDENT_CHECKER_MANIFEST" if all(gates.values()) else "FAIL"
    summary: JsonObject = {
        "schema_version": 1,
        "status": status,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "protocol_path": str(args.protocol).replace("\\", "/"),
        "protocol_sha256": _sha256(protocol_path),
        "source_run": protocol["source_run"],
        "execution_model": "fresh_python_process_per_case",
        "separation": {
            "approval_store": "persisted logical and physical plans",
            "execution_evidence_store": "persisted certificate and independent observation",
            "checker": (
                "separately launched process with schema validation and recomputation inputs"
            ),
        },
        "cases": rows,
        "gates": gates,
        "claim_boundary": protocol["claim_boundary"],
    }
    _write(out / "summary.json", summary)
    shutil.copy2(protocol_path, out / "frozen_protocol.json")
    print(status)
    print(out)


if __name__ == "__main__":
    main()
