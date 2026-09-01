"""Run the frozen cross-stage contract ablation.

This analysis asks whether logical approval, legality-first planning, and
evidence-bound checking provide substitutable guarantees.  It only composes
previously frozen observations; it does not execute DuckDB, refit an optimizer,
or alter any paper denominator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def _object(path: Path) -> JsonObject:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_state(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    )
    return commit, dirty


def _write_json(path: Path, payload: JsonObject) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[JsonObject]) -> None:
    fields = [
        "profile_id",
        "logical_approval",
        "legality_first_planning",
        "evidence_bound_checking",
        "unsafe_logical_acceptance_count",
        "false_logical_rejection_count",
        "illegal_physical_selection_count",
        "registered_faults_detected",
        "registered_fault_count",
        "cross_stage_contract_complete",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _profile(
    profile_id: str,
    *,
    logical_approval: bool,
    legality_first: bool,
    evidence_bound: bool,
    validator: JsonObject,
    planner: JsonObject,
    certificate: JsonObject,
) -> JsonObject:
    if logical_approval:
        validation = validator["trustaero_full"]
    else:
        validation = validator["policy_output_only"]

    illegal = 0
    for regime in planner.values():
        key = (
            "legality_first_illegal_selection_count"
            if legality_first
            else "governance_blind_illegal_selection_count"
        )
        illegal += int(regime[key])

    cert_profile = (
        certificate["trustaero_certificate_full"]
        if evidence_bound
        else certificate["ordinary_event_log"]
    )
    unsafe = int(validation["unsafe_acceptance_count"])
    false_reject = int(validation["false_reject_count"])
    detected = int(cert_profile["detected_fault_count"])
    fault_count = int(cert_profile["fault_count"])
    complete = unsafe == 0 and false_reject == 0 and illegal == 0 and detected == fault_count
    return {
        "profile_id": profile_id,
        "logical_approval": logical_approval,
        "legality_first_planning": legality_first,
        "evidence_bound_checking": evidence_bound,
        "unsafe_logical_acceptance_count": unsafe,
        "false_logical_rejection_count": false_reject,
        "illegal_physical_selection_count": illegal,
        "registered_faults_detected": detected,
        "registered_fault_count": fault_count,
        "cross_stage_contract_complete": complete,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("experiments/frozen/cross_stage_contract_ablation_protocol_v1_20260812.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol_path = root / args.protocol
    protocol = _object(protocol_path)
    source_path = root / protocol["source_summary"]
    source_sha256 = _sha256(source_path)
    if source_sha256 != protocol["source_summary_sha256"]:
        raise ValueError(
            "Frozen source digest mismatch: "
            f"expected {protocol['source_summary_sha256']}, got {source_sha256}"
        )
    source = _object(source_path)

    validator = source["validator_ablation"]["phase0"]["summaries"]
    planner = source["planner_architecture_ablation"]["regimes"]
    certificate = source["certificate_component_ablation"]["profiles"]
    profiles = [
        _profile(
            "policy_output_plus_blind_planner_plus_event_log",
            logical_approval=False,
            legality_first=False,
            evidence_bound=False,
            validator=validator,
            planner=planner,
            certificate=certificate,
        ),
        _profile(
            "approval_plus_blind_planner_plus_event_log",
            logical_approval=True,
            legality_first=False,
            evidence_bound=False,
            validator=validator,
            planner=planner,
            certificate=certificate,
        ),
        _profile(
            "approval_plus_legality_first_plus_event_log",
            logical_approval=True,
            legality_first=True,
            evidence_bound=False,
            validator=validator,
            planner=planner,
            certificate=certificate,
        ),
        _profile(
            "full_trustaero",
            logical_approval=True,
            legality_first=True,
            evidence_bound=True,
            validator=validator,
            planner=planner,
            certificate=certificate,
        ),
    ]
    gates = {
        "only_full_contract_complete": [
            row["profile_id"] for row in profiles if row["cross_stage_contract_complete"]
        ]
        == ["full_trustaero"],
        "logical_approval_closes_ua_fr": all(
            row["unsafe_logical_acceptance_count"] == 0
            and row["false_logical_rejection_count"] == 0
            for row in profiles
            if row["logical_approval"]
        ),
        "legality_first_closes_illegal_selection": all(
            row["illegal_physical_selection_count"] == 0
            for row in profiles
            if row["legality_first_planning"]
        ),
        "full_certificate_closes_registered_faults": all(
            row["registered_faults_detected"] == row["registered_fault_count"]
            for row in profiles
            if row["evidence_bound_checking"]
        ),
        "each_ablated_profile_has_a_distinct_gap": all(
            not row["cross_stage_contract_complete"] for row in profiles[:-1]
        ),
    }
    status = "PASS_CROSS_STAGE_CONTRACT_ABLATION" if all(gates.values()) else "FAIL"
    commit, dirty = _git_state(root)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = root / protocol["results_dir"] / run_id
    out.mkdir(parents=True)
    summary: JsonObject = {
        "schema_version": 1,
        "status": status,
        "analysis_type": "frozen_cross_stage_contract_ablation",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "protocol_path": str(args.protocol).replace("\\", "/"),
        "protocol_sha256": _sha256(protocol_path),
        "source_summary_path": str(source_path.relative_to(root)).replace("\\", "/"),
        "source_summary_sha256": _sha256(source_path),
        "profiles": profiles,
        "gates": gates,
        "claim_boundary": protocol["claim_boundary"],
        "denominators": {
            "logical_validation_cases": 11,
            "planner_decisions_per_policy": 96,
            "planner_policy_regimes": len(planner),
            "registered_certificate_faults": profiles[0]["registered_fault_count"],
        },
        "new_duckdb_runs": 0,
        "optimizer_refit": False,
        "paper_denominator_changes": False,
    }
    _write_json(out / "summary.json", summary)
    _write_csv(out / "profiles.csv", profiles)
    lines = [
        "# Cross-stage contract ablation",
        "",
        (
            "This frozen analysis tests whether approval, legality-first planning, and "
            "evidence-bound checking are substitutable."
        ),
        "",
        (
            "| Profile | UA | FR | Illegal physical selections | "
            "Registered faults detected | Complete contract |"
        ),
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for row in profiles:
        lines.append(
            f"| {row['profile_id']} | {row['unsafe_logical_acceptance_count']} | "
            f"{row['false_logical_rejection_count']} | {row['illegal_physical_selection_count']} | "
            f"{row['registered_faults_detected']}/{row['registered_fault_count']} | "
            f"{'yes' if row['cross_stage_contract_complete'] else 'no'} |"
        )
    lines.extend(["", "## Boundary", "", *[f"- {x}" for x in protocol["claim_boundary"]]])
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(status)
    print(out)


if __name__ == "__main__":
    main()
