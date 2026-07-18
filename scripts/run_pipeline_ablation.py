"""Run or resume the Phase 2M complete-pipeline ablation smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.pipeline_ablation import (
    load_pipeline_ablation_config,
    run_pipeline_ablation_smoke,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Versioned Phase 2M JSON config.")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume RUN_ID, or the latest run when omitted.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print completed scenarios, elapsed time, and ETA.",
    )
    args = parser.parse_args()
    config = load_pipeline_ablation_config(args.config)
    resume_run_id = args.resume
    if resume_run_id == "latest":
        latest_path = Path(config.results_dir) / "latest_run.json"
        if not latest_path.exists():
            raise SystemExit(f"No resumable Phase 2M run found at {latest_path}")
        resume_run_id = json.loads(latest_path.read_text(encoding="utf-8"))["run_id"]
    try:
        output = run_pipeline_ablation_smoke(
            config,
            resume_run_id=resume_run_id,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nPhase 2M stopped safely. Re-run with --resume.")
        raise SystemExit(130) from None
    print(output)


if __name__ == "__main__":
    main()
