"""Run, resume, or preflight the one-shot Phase 2G Optimizer V3 holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.mechanism_microbench import (
    load_mechanism_microbench_config,
    run_mechanism_microbench,
)
from trustaero.experiments.phase2g_holdout import (
    Phase2GPreflight,
    audit_phase2g_preflight,
    create_or_validate_one_shot_manifest,
    evaluate_phase2g_holdout,
)


def _print_preflight(preflight: Phase2GPreflight) -> None:
    print(f"Phase 2G preflight: {preflight.status}")
    for check in preflight.checks:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.code}: {check.message}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/phase2g_optimizer_v3_holdout_v1.json",
    )
    parser.add_argument(
        "--authorization",
        default="experiments/frozen/phase2g_optimizer_v3_authorization.json",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume the consumed one-shot run; omit RUN_ID to use latest_run.json.",
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Check authorization without creating the Phase 2G result directory.",
    )
    args = parser.parse_args()
    config_path = root / args.config
    authorization_path = root / args.authorization
    resume = args.resume is not None
    preflight = audit_phase2g_preflight(
        root,
        config_path,
        authorization_path,
        resume=resume,
    )
    _print_preflight(preflight)
    if preflight.status != "PASS":
        raise SystemExit(1)
    if args.preflight_only:
        print("Preflight only: no Phase 2G directory or label was created.")
        return

    config = load_mechanism_microbench_config(config_path)
    create_or_validate_one_shot_manifest(
        root,
        config_path,
        preflight,
        resume=resume,
    )
    resume_run_id = args.resume
    if resume_run_id == "latest":
        latest_path = root / config.results_dir / "latest_run.json"
        resume_run_id = str(json.loads(latest_path.read_text(encoding="utf-8"))["run_id"])
    try:
        run_dir = run_mechanism_microbench(
            config,
            resume_run_id=resume_run_id,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nPhase 2G stopped safely. Resume with --resume --progress.")
        raise SystemExit(130) from None

    evaluation_dir = evaluate_phase2g_holdout(root, config_path, run_dir)
    summary = json.loads((evaluation_dir / "summary.json").read_text(encoding="utf-8"))
    v3 = summary["schemes"]["optimizer_v3_frozen"]
    v1 = summary["schemes"]["optimizer_v1_frozen"]
    print(f"Phase 2G run: {run_dir}")
    print(f"Evaluation: {evaluation_dir}")
    print(f"Holdout gate passed: {summary['holdout_gate']['passes']}")
    print(
        "V3 vs V1 within3: {v3:.1%} vs {v1:.1%}; mean regret: "
        "{v3_mean:.3f}% vs {v1_mean:.3f}%".format(
            v3=v3["within_tie_rate"],
            v1=v1["within_tie_rate"],
            v3_mean=v3["mean_regret_percent"],
            v1_mean=v1["mean_regret_percent"],
        )
    )
    for claim, payload in summary["paired_claims"].items():
        print(f"Claim {claim}: authorized={payload['authorized']}")


if __name__ == "__main__":
    main()
