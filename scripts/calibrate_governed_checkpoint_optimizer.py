"""Calibrate and grouped-validate the EA-1 analytic checkpoint optimizer."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_checkpoint_optimizer_calibration import (
    calibrate_governed_checkpoint_optimizer,
    load_checkpoint_calibration_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_checkpoint_optimizer_v2.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_checkpoint_calibration_config(config_path)
    started = time.perf_counter()

    def progress(done: int, total: int, label: str) -> None:
        if args.progress:
            elapsed = time.perf_counter() - started
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"[EA-Optimizer {done:02d}/{total:02d}] held out {label} "
                f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )

    output = calibrate_governed_checkpoint_optimizer(
        config, project_root=root, progress_callback=progress
    )
    result_path = output / "calibration.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metrics = result["metrics"]
    print(f"Status: {result['status']}", flush=True)
    print(
        "Confidence family hit: "
        f"optimizer={metrics['confidence_family_hit_rate']:.3f} "
        f"best-fixed={metrics['best_fixed_confidence_family_hit_rate']:.3f}",
        flush=True,
    )
    print(
        "Diagnostic regret: "
        f"mean={metrics['mean_diagnostic_median_regret_percent']:.3f}% "
        f"P95={metrics['p95_diagnostic_median_regret_percent']:.3f}% "
        f"max={metrics['maximum_diagnostic_median_regret_percent']:.3f}%",
        flush=True,
    )
    print(f"Result: {result_path}", flush=True)


if __name__ == "__main__":
    main()
