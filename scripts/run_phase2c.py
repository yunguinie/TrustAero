"""Run or resume Phase 2C stability and scale experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.phase2c import load_phase2c_config, run_phase2c


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/phase2c_calibration.json",
        help="Phase 2C JSON configuration file.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume RUN_ID, or the latest run when no ID is supplied.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Render an in-terminal progress bar and ETA.",
    )
    args = parser.parse_args()
    config = load_phase2c_config(args.config)
    resume_run_id = args.resume
    if resume_run_id == "latest":
        latest_path = Path(config.results_dir) / "latest_run.json"
        if not latest_path.exists():
            raise SystemExit(f"No resumable Phase 2C run found at {latest_path}")
        resume_run_id = json.loads(latest_path.read_text(encoding="utf-8"))["run_id"]
    output_dir = run_phase2c(
        config,
        resume_run_id=resume_run_id,
        show_progress=args.progress,
    )
    print(output_dir)


if __name__ == "__main__":
    main()
