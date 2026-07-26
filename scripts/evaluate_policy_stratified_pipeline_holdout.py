"""Evaluate the frozen optimizer across three governance policy regimes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.policy_stratified_pipeline_holdout import (
    evaluate_policy_stratified_pipeline_holdout,
    load_policy_stratified_holdout_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    parser.add_argument(
        "--config",
        default="experiments/configs/policy_stratified_pipeline_holdout_evaluation_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_policy_stratified_holdout_config(root / args.config)
    run_id = args.run
    if run_id == "latest":
        latest = root / config.measurement_results_dir / "latest_run.json"
        run_id = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    result_path = evaluate_policy_stratified_pipeline_holdout(
        config,
        project_root=root,
        measurement_run_dir=root / config.measurement_results_dir / run_id,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    primary = result["regime_results"][result["primary_adaptive_policy_id"]]
    metrics = primary["optimizer_metrics"]
    fixed = primary["best_fixed_metrics"]
    print(f"Status: {result['status']}")
    print(
        "No-raw-join optimizer: "
        f"hit={metrics['oracle_set_hit_rate']:.3f} "
        f"mean={metrics['mean_regret_percent']:.3f}% "
        f"P95={metrics['p95_regret_percent']:.3f}% "
        f"max={metrics['maximum_regret_percent']:.3f}%"
    )
    print(
        f"Best fixed ({primary['best_fixed_candidate_id']}): "
        f"hit={fixed['oracle_set_hit_rate']:.3f} "
        f"mean={fixed['mean_regret_percent']:.3f}% "
        f"P95={fixed['p95_regret_percent']:.3f}%"
    )
    print(f"Result: {result_path.resolve()}")


if __name__ == "__main__":
    main()
