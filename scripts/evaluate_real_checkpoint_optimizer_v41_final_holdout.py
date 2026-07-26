"""Evaluate frozen V4.1 once against the untouched real-month holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_checkpoint_optimizer_validation import (
    evaluate_real_checkpoint_optimizer_validation,
    load_real_checkpoint_validation_config,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_run", help="Measurement run path or 'latest'.")
    parser.add_argument(
        "--config",
        default=("experiments/configs/real_checkpoint_optimizer_v41_final_holdout_evaluation.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_real_checkpoint_validation_config(root / args.config)
    source = args.source_run
    if source == "latest":
        measurement = json.loads(
            (root / config.measurement_config_path).read_text(encoding="utf-8")
        )
        latest = json.loads(
            (root / measurement["results_dir"] / "latest_run.json").read_text(encoding="utf-8")
        )
        source = str(root / measurement["results_dir"] / str(latest["run_id"]))
    output = evaluate_real_checkpoint_optimizer_validation(
        config,
        source_run_dir=source,
        project_root=root,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    analytic = result["analytic_metrics"]
    threshold = result["frozen_threshold_metrics"]
    print(f"Status: {result['status']}")
    print(
        "Analytic V4.1: "
        f"hit={analytic['confidence_family_hit_rate']:.3f} "
        f"mean={analytic['mean_regret_percent']:.3f}% "
        f"P95={analytic['p95_regret_percent']:.3f}% "
        f"max={analytic['max_regret_percent']:.3f}%"
    )
    print(
        "Frozen threshold: "
        f"hit={threshold['confidence_family_hit_rate']:.3f} "
        f"mean={threshold['mean_regret_percent']:.3f}% "
        f"P95={threshold['p95_regret_percent']:.3f}% "
        f"max={threshold['max_regret_percent']:.3f}%"
    )
    print(f"Result: {output / 'evaluation.json'}")


if __name__ == "__main__":
    main()
