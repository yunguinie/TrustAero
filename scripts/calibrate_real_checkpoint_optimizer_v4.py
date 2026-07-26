"""Calibrate the real-data V4 checkpoint optimizer with grouped validation."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.real_checkpoint_optimizer_calibration import (
    calibrate_real_checkpoint_optimizer,
    load_real_checkpoint_calibration_config,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="experiments/configs/real_checkpoint_optimizer_v4_calibration.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_real_checkpoint_calibration_config(root / args.config)

    def progress(done: int, total: int, group: str) -> None:
        if args.progress:
            print(
                f"[Real-V4 {done:02d}/{total:02d}] held out {group}",
                flush=True,
            )

    started = time.perf_counter()
    output = calibrate_real_checkpoint_optimizer(
        config, project_root=root, progress_callback=progress
    )
    result = json.loads((output / "calibration.json").read_text(encoding="utf-8"))
    analytic = result["analytic_metrics"]
    threshold = result["learned_threshold_metrics"]
    print(f"Completed in {time.perf_counter() - started:.1f}s")
    print(f"Status: {result['status']}")
    print(
        "Analytic: "
        f"hit={analytic['confidence_family_hit_rate']:.3f} "
        f"mean={analytic['mean_regret_percent']:.3f}% "
        f"P95={analytic['p95_regret_percent']:.3f}% "
        f"max={analytic['max_regret_percent']:.3f}%"
    )
    print(
        "Learned threshold: "
        f"hit={threshold['confidence_family_hit_rate']:.3f} "
        f"mean={threshold['mean_regret_percent']:.3f}% "
        f"P95={threshold['p95_regret_percent']:.3f}% "
        f"max={threshold['max_regret_percent']:.3f}%"
    )
    print(f"Result: {output / 'calibration.json'}")


if __name__ == "__main__":
    main()
