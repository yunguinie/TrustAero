"""Verify that a certificate from the base snapshot cannot attest a refreshed snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.ir.enums import ReasonCode
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    GovernedExecutionCertificate,
    ValidatedLogicalPlan,
)
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)


def load_model(path: Path, model):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_run", type=Path)
    parser.add_argument("refreshed_run", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = args.base_run if args.base_run.is_absolute() else root / args.base_run
    refreshed = (
        args.refreshed_run if args.refreshed_run.is_absolute() else root / args.refreshed_run
    )
    plan = load_model(refreshed / "validated_plan.json", ValidatedLogicalPlan)
    physical = load_model(refreshed / "approved_physical_plan.json", ApprovedPhysicalPlan)
    certificate = load_model(base / "certificate.json", GovernedExecutionCertificate)
    check = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest=certificate.result_digest,
        observed_planner_decision_digest=physical.planner_decision_digest,
        observed_planner_selected_candidate_id=physical.planner_selected_candidate_id,
    )
    codes = sorted({item.code.value for item in check.diagnostics})
    expected = ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH.value
    if check.status != CertificateVerificationStatus.REJECT or expected not in codes:
        raise SystemExit(
            f"Expected cross-snapshot rejection with {expected}; "
            f"observed status={check.status.value}, codes={codes}"
        )
    base_summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
    refreshed_summary = json.loads((refreshed / "summary.json").read_text(encoding="utf-8"))
    output = {
        "schema_version": 1,
        "status": "PASS_CROSS_SNAPSHOT_CERTIFICATE_REJECTION",
        "base_run": base.relative_to(root).as_posix(),
        "refreshed_run": refreshed.relative_to(root).as_posix(),
        "base_result_digest": base_summary["execution"]["result_digest"],
        "refreshed_result_digest": refreshed_summary["execution"]["result_digest"],
        "result_digest_changed": (
            base_summary["execution"]["result_digest"]
            != refreshed_summary["execution"]["result_digest"]
        ),
        "base_data_snapshots": certificate.data_snapshots,
        "refreshed_data_snapshots": plan.bindings.data_snapshots,
        "verification_status": check.status.value,
        "diagnostic_codes": codes,
        "expected_diagnostic": expected,
        "claim_boundary": (
            "This is a version-binding regression across two deterministic snapshots, "
            "not cross-dataset generalization or a cryptographic execution proof."
        ),
    }
    path = refreshed / "cross_snapshot_certificate_check.json"
    path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Status: {output['status']}")
    print(f"Result: {path}")


if __name__ == "__main__":
    main()
