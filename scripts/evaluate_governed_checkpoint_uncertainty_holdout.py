"""Evaluate the frozen V3.1 guard on its untouched holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.governed_checkpoint_uncertainty_holdout import (
    evaluate_uncertainty_holdout,
    load_uncertainty_holdout_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run_dir", help="Completed holdout run, or 'latest'")
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_checkpoint_uncertainty_holdout_evaluation_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_uncertainty_holdout_config(root / args.config)
    source = args.source_run_dir
    if source == "latest":
        measurement_config = json.loads(
            (
                root / "experiments/configs/governed_checkpoint_uncertainty_holdout_v1.json"
            ).read_text(encoding="utf-8")
        )
        results_root = root / measurement_config["results_dir"]
        latest = json.loads((results_root / "latest_run.json").read_text(encoding="utf-8"))
        source = str(results_root / latest["run_id"])
    output = evaluate_uncertainty_holdout(config, source_run_dir=source, project_root=root)
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
