"""Run or resume development-only Optimizer V4 operator profiles."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from trustaero.experiments.optimizer_v4_profiles import (
    load_optimizer_v4_profile_config,
    run_optimizer_v4_profiles,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/optimizer_v4_profiles_development_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run one family/profile under a separate non-formal result directory.",
    )
    args = parser.parse_args()
    config_path = root / args.config
    config = load_optimizer_v4_profile_config(config_path)
    if args.smoke:
        config = replace(
            config,
            protocol_name=config.protocol_name + "_smoke",
            results_dir="results/optimizer_v4_profiles_smoke",
            identifier_widths=config.identifier_widths[:1],
            target_match_rates=config.target_match_rates[:1],
            profile_runs=1,
            require_clean_git=False,
        )
    try:
        run_dir = run_optimizer_v4_profiles(
            config,
            project_root=root,
            config_path=config_path,
            resume_run_id=args.resume,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nStopped safely. Resume the full run with --resume --progress.")
        raise SystemExit(130) from None
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"\nRun directory: {run_dir}")
    print(
        "V4 profile gate: {status}; families={families}; profiles={profiles}; spill={spill}".format(
            status=summary["status"],
            families=summary["family_count"],
            profiles=summary["profile_count"],
            spill=summary["spilled_profile_count"],
        )
    )


if __name__ == "__main__":
    main()
