"""Run or resume the progress-visible real-data infrastructure pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_data_pilot import (
    load_real_data_pilot_config,
    run_real_data_pilot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/real_data_pilot.json",
        help="Pilot JSON configuration.",
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
        help="Show completed steps, elapsed time, and ETA in the terminal.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_real_data_pilot_config(args.config)
    resume_run_id = args.resume
    if resume_run_id == "latest":
        latest = root / config.results_dir / "latest_run.json"
        if not latest.is_file():
            raise SystemExit(f"No resumable run found at {latest}")
        resume_run_id = json.loads(latest.read_text(encoding="utf-8"))["run_id"]
    try:
        output = run_real_data_pilot(
            config,
            project_root=root,
            resume_run_id=resume_run_id,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nPilot stopped safely. Re-run the same command with --resume.")
        raise SystemExit(130) from None
    print(f"Real-data infrastructure pilot completed: {output}")


if __name__ == "__main__":
    main()
