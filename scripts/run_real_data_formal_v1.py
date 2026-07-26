"""Run the frozen BTS/NYC formal development-partition measurement suite.

The terminal shows progress for every candidate execution. Results are method-
level paper candidates, but January is explicitly not an unseen optimizer holdout.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file
from trustaero.experiments.bts_mask_join_analysis import analyze_bts_mask_join_pilot
from trustaero.experiments.bts_mask_join_pilot import (
    load_bts_mask_join_pilot_config,
    run_bts_mask_join_pilot,
)
from trustaero.experiments.real_data_candidate_analysis import (
    analyze_real_data_candidate_pilot,
)
from trustaero.experiments.real_data_candidate_pilot import (
    load_candidate_pilot_config,
    run_real_data_candidate_pilot,
)
from trustaero.experiments.real_data_formal_protocol import (
    validate_formal_real_data_protocol,
)
from trustaero.experiments.real_data_governed import _atomic_json
from trustaero.experiments.real_data_pilot import _git_state
from trustaero.reproducibility import audit_source_freeze


def _component_record(run_dir: Path, acceptance: dict[str, Any]) -> dict[str, Any]:
    """Bind a completed component without copying its potentially large CSV."""

    return {
        "run_directory": str(run_dir),
        "summary_sha256": sha256_file(run_dir / "summary.json"),
        "measurements_sha256": sha256_file(run_dir / "measurements.csv"),
        "acceptance_sha256": sha256_file(run_dir / "acceptance.json"),
        "status": acceptance["status"],
        "paper_performance_evidence": acceptance["paper_performance_evidence"],
        "heldout_optimizer_evidence": acceptance["heldout_optimizer_evidence"],
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        default="experiments/configs/real_data_formal_protocol_v1.json",
    )
    parser.add_argument(
        "--component",
        choices=("all", "candidate", "mask_join"),
        default="all",
        help="Run the complete suite or one restartable component.",
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--resume-candidate", metavar="RUN_ID")
    args = parser.parse_args()

    freeze = audit_source_freeze(root)
    if freeze.status != "READY":
        raise SystemExit("Formal suite refused: source-freeze status is not READY.")
    protocol = validate_formal_real_data_protocol(
        root / args.protocol,
        project_root=root,
    )
    commit, dirty = _git_state(root)
    if dirty:
        raise SystemExit("Formal suite refused: Git worktree is dirty.")

    components: dict[str, dict[str, Any]] = {}
    if args.component in {"all", "candidate"}:
        candidate_config = load_candidate_pilot_config(
            root / "experiments/configs/real_data_formal_candidate_v1.json"
        )
        print("\n[1/2] Running BTS masked-read and NYC zone-aggregate candidates")
        candidate_dir = run_real_data_candidate_pilot(
            candidate_config,
            project_root=root,
            resume_run_id=args.resume_candidate,
            show_progress=args.progress,
        )
        candidate_acceptance = analyze_real_data_candidate_pilot(candidate_dir)
        components["full-month-materialization"] = _component_record(
            candidate_dir, candidate_acceptance
        )
        print(f"Candidate component: {candidate_acceptance['status']} ({candidate_dir})")

    if args.component in {"all", "mask_join"}:
        mask_config = load_bts_mask_join_pilot_config(
            root / "experiments/configs/bts_mask_join_formal_v1.json"
        )
        print("\n[2/2] Running BTS early/late Mask placement")
        mask_dir = run_bts_mask_join_pilot(
            mask_config,
            project_root=root,
            show_progress=args.progress,
        )
        mask_acceptance = analyze_bts_mask_join_pilot(mask_dir)
        components["full-month-mask-placement"] = _component_record(mask_dir, mask_acceptance)
        print(f"Mask/Join component: {mask_acceptance['status']} ({mask_dir})")

    bundle_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    bundle_dir = root / "results/real_data_formal_v1" / bundle_id
    passed = bool(components) and all(item["status"] == "PASS" for item in components.values())
    bundle = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "status": "PASS" if passed else "FAIL",
        "source_commit": commit,
        "source_freeze_status": freeze.status,
        "formal_protocol_id": protocol.protocol_id,
        "formal_protocol_sha256": protocol.protocol_sha256,
        "paper_performance_evidence": True,
        "heldout_optimizer_evidence": False,
        "optimizer_selection_evaluated": False,
        "components": components,
        "scientific_boundary": protocol.scientific_boundary,
    }
    _atomic_json(bundle_dir / "summary.json", bundle)
    _atomic_json(
        root / "results/real_data_formal_v1/latest_run.json",
        {"bundle_id": bundle_id},
    )
    print(f"\nFormal suite {bundle['status']}: {bundle_dir / 'summary.json'}")
    if bundle["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
