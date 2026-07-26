"""Analyze a V5 V2 calibration with frozen pollution-safe paired inference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v5_calibration_analysis import (
    analyze_optimizer_v5_calibration,
    load_v5_calibration_inference_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/optimizer_v5_calibration_inference_v2.json"
    config = load_v5_calibration_inference_config(config_path)
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    else:
        results = root / "results/optimizer_v5_real_candidate_calibration_v2"
        latest = json.loads((results / "latest_run.json").read_text(encoding="utf-8"))
        run_dir = results / str(latest["run_id"])
    result = analyze_optimizer_v5_calibration(
        run_dir,
        config,
        project_root=root,
    )
    print(f"Status: {result['status']}")
    print(f"Model-eligible units: {result['model_eligible_unit_count']}")
    print(f"Result: {run_dir / 'v5_inference.json'}")


if __name__ == "__main__":
    main()
