"""Evaluate the frozen optimizer once on the latest BTS/NYC transfer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_governed_pipeline_transfer import (
    evaluate_real_governed_pipeline_transfer,
    load_real_governed_pipeline_evaluation_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    parser.add_argument(
        "--config",
        default="experiments/configs/real_governed_pipeline_transfer_evaluation_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_real_governed_pipeline_evaluation_config(root / args.config)
    run_id = args.run
    if run_id == "latest":
        latest = root / config.measurement_results_dir / "latest_run.json"
        run_id = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    result_path = evaluate_real_governed_pipeline_transfer(
        config,
        project_root=root,
        measurement_run_dir=root / config.measurement_results_dir / run_id,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metrics = result["real_transfer_metrics"]
    print(f"Status: {result['status']}")
    print(
        "Frozen model: "
        f"hit={metrics['oracle_set_hit_rate']:.3f} "
        f"mean={metrics['mean_regret_percent']:.3f}% "
        f"P95={metrics['p95_regret_percent']:.3f}% "
        f"max={metrics['maximum_regret_percent']:.3f}%"
    )
    print(f"Best fixed: {result['best_fixed_candidate_id']}")
    print(f"Result: {result_path.resolve()}")


if __name__ == "__main__":
    main()
