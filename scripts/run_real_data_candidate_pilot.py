"""Run or resume the approved real-data multi-candidate performance pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_data_candidate_pilot import (
    load_candidate_pilot_config,
    run_real_data_candidate_pilot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/real_data_candidate_pilot.json",
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_candidate_pilot_config(args.config)
    resume = args.resume
    if resume == "latest":
        latest_path = root / config.results_dir / "latest_run.json"
        resume = str(json.loads(latest_path.read_text(encoding="utf-8"))["run_id"])
    try:
        output = run_real_data_candidate_pilot(
            config,
            project_root=root,
            resume_run_id=resume,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nCandidate pilot stopped safely; rerun with --resume.")
        raise SystemExit(130) from None
    print(f"Real-data candidate pilot completed: {output}")


if __name__ == "__main__":
    main()
