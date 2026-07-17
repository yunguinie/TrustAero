"""Run or resume DuckDB mechanism microbenchmarks with visible progress."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.mechanism_microbench import (
    load_mechanism_microbench_config,
    run_mechanism_microbench,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/phase2h_mechanism_pilot.json",
        help="Versioned mechanism-microbenchmark JSON configuration.",
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
        help="Print completed units, elapsed time, and ETA in the terminal.",
    )
    args = parser.parse_args()
    config = load_mechanism_microbench_config(args.config)
    resume_run_id = args.resume
    if resume_run_id == "latest":
        latest_path = Path(config.results_dir) / "latest_run.json"
        if not latest_path.exists():
            raise SystemExit(f"No resumable mechanism run found at {latest_path}")
        resume_run_id = json.loads(latest_path.read_text(encoding="utf-8"))["run_id"]
    try:
        output_dir = run_mechanism_microbench(
            config,
            resume_run_id=resume_run_id,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nMicrobenchmark stopped safely. Re-run with --resume.")
        raise SystemExit(130) from None
    print(output_dir)


if __name__ == "__main__":
    main()
