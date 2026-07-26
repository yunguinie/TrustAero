"""Evaluate the frozen governed pipeline model on one unseen measurement run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.governed_pipeline_cost_holdout import (
    evaluate_governed_pipeline_cost_holdout,
    load_governed_pipeline_cost_holdout_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    parser.add_argument(
        "--config",
        default=("experiments/configs/governed_pipeline_cost_holdout_evaluation_v1.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_governed_pipeline_cost_holdout_config(config_path)
    measurement_root = root / config.measurement_results_dir
    run_id = args.run
    if run_id == "latest":
        latest = json.loads((measurement_root / "latest_run.json").read_text(encoding="utf-8"))
        run_id = str(latest["run_id"])
    output = evaluate_governed_pipeline_cost_holdout(
        config,
        project_root=root,
        measurement_run_dir=measurement_root / run_id,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    metrics = result["holdout_metrics"]
    print(f"Status: {result['status']}")
    print(
        "Frozen model: "
        f"hit={metrics['oracle_set_hit_rate']:.3f} "
        f"mean={metrics['mean_regret_percent']:.3f}% "
        f"P95={metrics['p95_regret_percent']:.3f}% "
        f"max={metrics['maximum_regret_percent']:.3f}%"
    )
    print(f"Best fixed: {result['best_fixed_candidate_id']}")
    print(f"Result: {(output / 'evaluation.json').resolve()}")


if __name__ == "__main__":
    main()
