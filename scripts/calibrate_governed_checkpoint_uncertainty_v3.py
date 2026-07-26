"""Calibrate the grouped one-sided V3 checkpoint uncertainty guard."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_checkpoint_uncertainty_calibration import (
    calibrate_checkpoint_uncertainty_guard,
    load_checkpoint_uncertainty_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_checkpoint_uncertainty_v3_1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_checkpoint_uncertainty_config(root / args.config)
    started = time.perf_counter()

    def progress(completed: int, total: int, label: str) -> None:
        if not args.progress:
            return
        elapsed = time.perf_counter() - started
        eta = elapsed / completed * (total - completed) if completed else 0.0
        print(
            f"[EA-V3 {completed:02d}/{total:02d}] {label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    output = calibrate_checkpoint_uncertainty_guard(
        config, project_root=root, progress_callback=progress
    )
    result = json.loads((output / "calibration.json").read_text(encoding="utf-8"))
    metrics = result["metrics"]
    print(f"Status: {result['status']}")
    print(
        "Guard: "
        f"upper-error={metrics['query_margin_error_upper_ms']:.3f}ms "
        f"family-hit={metrics['confidence_family_hit_rate']:.3f}"
    )
    print(
        "Diagnostic regret: "
        f"mean={metrics['mean_diagnostic_median_regret_percent']:.3f}% "
        f"P95={metrics['p95_diagnostic_median_regret_percent']:.3f}% "
        f"max={metrics['maximum_diagnostic_median_regret_percent']:.3f}%"
    )
    print(f"Result: {output / 'calibration.json'}")


if __name__ == "__main__":
    main()
