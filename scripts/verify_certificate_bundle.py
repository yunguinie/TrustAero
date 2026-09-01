"""Verify a persisted approval manifest and execution evidence in a fresh process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    GovernedExecutionCertificate,
    ValidatedLogicalPlan,
)
from trustaero.validator.certificate import verify_execution_certificate


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logical-plan", type=Path, required=True)
    parser.add_argument("--physical-plan", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    logical = ValidatedLogicalPlan.model_validate_json(
        args.logical_plan.read_text(encoding="utf-8")
    )
    physical = ApprovedPhysicalPlan.model_validate_json(
        args.physical_plan.read_text(encoding="utf-8")
    )
    certificate = GovernedExecutionCertificate.model_validate_json(
        args.certificate.read_text(encoding="utf-8")
    )
    observation = _load(args.observation)
    verification = verify_execution_certificate(
        logical,
        physical,
        certificate,
        observed_result_digest=str(observation["result_digest"]),
        observed_planner_decision_digest=str(observation["planner_decision_digest"]),
        observed_planner_selected_candidate_id=str(observation["planner_selected_candidate_id"]),
    )
    result = {
        "status": verification.status.value,
        "verified_obligations": [item.value for item in verification.verified_obligations],
        "diagnostics": [item.model_dump(mode="json") for item in verification.diagnostics],
        "unverified_components": list(verification.unverified_components),
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
