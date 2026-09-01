"""Verify the structure and headline metrics of the committed artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: str) -> dict[str, Any]:
    value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_manifest() -> None:
    manifest = load_json("artifact/manifest.json")
    experiments = manifest.get("experiments")
    expect(isinstance(experiments, list) and experiments, "manifest has no experiments")
    ids: set[str] = set()
    for row in experiments:
        expect(isinstance(row, dict), "manifest experiment must be an object")
        experiment_id = str(row["id"])
        expect(experiment_id not in ids, f"duplicate experiment id: {experiment_id}")
        ids.add(experiment_id)
        for field in ("protocol", "result", "runner"):
            path = ROOT / str(row[field])
            expect(path.is_file(), f"missing {field} for {experiment_id}: {path}")


def verify_metrics() -> None:
    phase0 = load_json("artifact/results/rq1-phase0/summary.json")
    expect(phase0["case_count"] == 26 and phase0["all_correct"] is True, "RQ1 suite")

    blackbox = load_json("artifact/results/rq1-blackbox/primary_result.json")["metrics"]
    expect(blackbox["evaluated_cases"] == 1000, "black-box case count")
    expect(blackbox["unsafe_acceptance_count"] == 0, "black-box unsafe acceptance")
    expect(blackbox["false_rejection_count"] == 0, "black-box false rejection")
    expect(blackbox["unexpected_exception_count"] == 0, "black-box exceptions")

    scalability = load_json("artifact/results/rq1-validator-scalability/summary.json")
    expect(scalability["case_count"] == 24, "validator scalability configuration count")
    expect(scalability["exception_count"] == 0, "validator scalability exceptions")

    pipeline = load_json("artifact/results/rq2-governed-pipeline/evaluation.json")
    expect(pipeline["illegal_selections"] == [], "governed-pipeline illegal selections")

    lineage = load_json("artifact/results/rq2-lineage-checkpoint/evaluation.json")["model"]
    expect(lineage["decision_count"] == 18, "lineage-checkpoint decision count")
    expect(lineage["oracle_set_hit_rate"] == 1.0, "lineage-checkpoint oracle-set hit rate")

    third = load_json("artifact/results/rq2-third-family/sf10-primary/summary.json")
    expect(third["planner_quality"]["within_3_percent_count"] == 11, "third family holdout")

    space = load_json("artifact/results/rq2-candidate-scalability/summary.json")
    expect(space["planning_trial_count"] == 72000, "candidate-space trial count")
    expect(all(space["gates"].values()), "candidate-space gates")

    certificate = load_json("artifact/results/rq3-certificate/certificate_component_ablation.json")
    full = certificate["profiles"]["trustaero_certificate_full"]
    expect(full["detected_fault_count"] == 19 and full["fault_count"] == 19, "Certificate")

    findings = load_json("artifact/results/rq3-lineage-1m/evaluation.json")["unit_findings"]
    million = next(row for row in findings if row["row_count"] == 1_000_000)
    expect(million["row_count"] == 1_000_000, "million-row Lineage row count")
    expect(abs(million["bytes_per_edge"] - 32.000462) < 1e-9, "Lineage bytes per edge")

    multisource = load_json("artifact/results/rq3-four-source/summary.json")
    expect(multisource["execution"]["row_count"] == 9128, "four-source row count")
    expect(multisource["lineage"]["source_count"] == 4, "four-source source count")

    cross_stage = load_json("artifact/results/rq4-cross-stage/summary.json")
    full_chain = cross_stage["profiles"][-1]
    expect(full_chain["illegal_physical_selection_count"] == 0, "full-chain legality")
    expect(full_chain["registered_faults_detected"] == 19, "full-chain fault detection")


def verify_checksums() -> None:
    checksum_file = ROOT / "artifact/checksums.sha256"
    expect(checksum_file.is_file(), "missing artifact/checksums.sha256")
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        path = ROOT / relative
        expect(path.is_file(), f"missing checksummed file: {relative}")
        # Git stores these JSON/CSV/Markdown artifacts with LF endings. Normalize
        # a Windows working tree before hashing so verification is cross-platform.
        content = path.read_bytes().removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
        actual = hashlib.sha256(content).hexdigest()
        expect(actual == expected, f"checksum mismatch: {relative}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-checksums", action="store_true")
    args = parser.parse_args()
    verify_manifest()
    verify_metrics()
    if not args.skip_checksums:
        verify_checksums()
    print("TrustAero artifact verification: PASS")


if __name__ == "__main__":
    main()
