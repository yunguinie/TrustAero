"""Run or resume the expanded January Optimizer V4 calibration matrix."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from trustaero.experiments.optimizer_v4_calibration import (
    load_optimizer_v4_calibration_config,
    run_optimizer_v4_calibration,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/optimizer_v4_calibration_development_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    config_path = root / args.config
    config = load_optimizer_v4_calibration_config(config_path)
    if args.smoke:
        config = replace(
            config,
            protocol_name=config.protocol_name + "_smoke",
            results_dir="results/optimizer_v4_calibration_smoke",
            windows=config.windows[:1],
            identifier_widths=config.identifier_widths[:1],
            target_match_rates=config.target_match_rates[:1],
            warmup_blocks=0,
            measured_blocks=2,
            require_clean_git=False,
        )
    try:
        run_dir = run_optimizer_v4_calibration(
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
        "V4 calibration: {status}; groups={groups}; families={families}; "
        "measurements={measurements}".format(
            status=summary["status"],
            groups=summary["scenario_group_count"],
            families=summary["family_count"],
            measurements=summary["measurement_count"],
        )
    )


if __name__ == "__main__":
    main()
