"""Run or resume the January real-data Optimizer V3 transfer gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_optimizer_transfer import (
    load_real_optimizer_transfer_config,
    run_real_optimizer_transfer,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/real_optimizer_transfer_development_v1.json",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume a technical interruption; omit RUN_ID to use latest_run.json.",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    config = load_real_optimizer_transfer_config(root / args.config)
    try:
        run_dir = run_real_optimizer_transfer(
            config,
            project_root=root,
            config_path=root / args.config,
            resume_run_id=args.resume,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nStopped safely. Resume with --resume --progress.")
        raise SystemExit(130) from None
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    metrics = summary["optimizer_v3_metrics"]
    print(f"\nRun directory: {run_dir}")
    print(f"Transfer gate: {summary['status']}")
    print(
        "V3 within3={within:.1%}, mean regret={mean:.3f}%, max regret={maximum:.3f}%, "
        "direct coverage={coverage:.1%}".format(
            within=metrics["within_3_percent_rate"],
            mean=metrics["mean_regret_percent"],
            maximum=metrics["max_regret_percent"],
            coverage=metrics["direct_model_coverage"],
        )
    )


if __name__ == "__main__":
    main()
