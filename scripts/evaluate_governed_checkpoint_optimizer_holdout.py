"""Evaluate the frozen EA-1 optimizer on one untouched holdout run."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    evaluate_governed_checkpoint_optimizer_holdout,
    load_checkpoint_holdout_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_run_dir",
        help="Completed frozen holdout run directory, or 'latest'",
    )
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_checkpoint_optimizer_holdout_evaluation_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_checkpoint_holdout_config(root / args.config)
    source_run_dir = args.source_run_dir
    if source_run_dir == "latest":
        measurement_config = json.loads(
            (root / "experiments/configs/governed_checkpoint_optimizer_holdout_v1.json").read_text(
                encoding="utf-8"
            )
        )
        results_root = root / measurement_config["results_dir"]
        latest = json.loads((results_root / "latest_run.json").read_text(encoding="utf-8"))
        source_run_dir = str(results_root / latest["run_id"])
    started = time.perf_counter()

    def progress(completed: int, total: int, label: str) -> None:
        if not args.progress:
            return
        elapsed = time.perf_counter() - started
        eta = elapsed / completed * (total - completed) if completed else 0.0
        print(
            f"[EA-Holdout {completed:02d}/{total:02d}] {label} "
            f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    output = evaluate_governed_checkpoint_optimizer_holdout(
        config,
        source_run_dir=source_run_dir,
        project_root=root,
        progress_callback=progress,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    metrics = result["metrics"]
    print(f"Status: {result['status']}")
    print(
        "Confidence family hit: "
        f"optimizer={metrics['confidence_family_hit_rate']:.3f} "
        f"best-fixed={metrics['best_fixed_confidence_family_hit_rate']:.3f}"
    )
    print(
        "Diagnostic regret: "
        f"mean={metrics['mean_diagnostic_median_regret_percent']:.3f}% "
        f"P95={metrics['p95_diagnostic_median_regret_percent']:.3f}% "
        f"max={metrics['maximum_diagnostic_median_regret_percent']:.3f}%"
    )
    print(f"Result: {output / 'evaluation.json'}")


if __name__ == "__main__":
    main()
