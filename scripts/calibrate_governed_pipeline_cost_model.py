"""Calibrate the governed pipeline physical-work cost model."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_pipeline_cost_calibration import (
    calibrate_governed_pipeline_cost_model,
    load_governed_pipeline_cost_calibration_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_pipeline_cost_calibration_v2.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    requested = Path(args.config)
    config_path = requested if requested.is_absolute() else root / requested
    config = load_governed_pipeline_cost_calibration_config(config_path)

    def report(done: int, total: int, label: str) -> None:
        if args.progress:
            print(
                f"[Pipeline-Cost {done:02d}/{total:02d}] held out {label}",
                flush=True,
            )

    started = time.perf_counter()
    output = calibrate_governed_pipeline_cost_model(
        config,
        project_root=root,
        progress_callback=report,
    )
    result = json.loads((output / "calibration.json").read_text(encoding="utf-8"))
    validation = result["grouped_validation"]
    print(f"Completed in {time.perf_counter() - started:.1f}s")
    print(f"Status: {result['status']}")
    print(
        "Grouped CV: "
        f"hit={validation['oracle_set_hit_rate']:.3f} "
        f"mean={validation['mean_regret_percent']:.3f}% "
        f"P95={validation['p95_regret_percent']:.3f}% "
        f"max={validation['maximum_regret_percent']:.3f}%"
    )
    print(f"Best fixed: {result['best_fixed_candidate_id']}")
    print(f"Result: {(output / 'calibration.json').resolve()}")


if __name__ == "__main__":
    main()
