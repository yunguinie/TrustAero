"""Score the frozen V3.1 optimizer on a real-distribution transfer run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_governed_checkpoint_transfer import (
    evaluate_real_governed_checkpoint_transfer,
    load_real_transfer_evaluation_config,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_run", help="Measurement run path or 'latest'.")
    parser.add_argument(
        "--config",
        default="experiments/configs/real_governed_checkpoint_transfer_evaluation_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_real_transfer_evaluation_config(root / args.config)
    source = args.source_run
    if source == "latest":
        measurement_config = json.loads(
            (root / "experiments/configs/real_governed_checkpoint_transfer_v1.json").read_text(
                encoding="utf-8"
            )
        )
        latest = json.loads(
            (root / measurement_config["results_dir"] / "latest_run.json").read_text(
                encoding="utf-8"
            )
        )
        source = str(root / measurement_config["results_dir"] / str(latest["run_id"]))
    output = evaluate_real_governed_checkpoint_transfer(
        config, source_run_dir=source, project_root=root
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    metrics = result["metrics"]
    print(f"Status: {result['status']}")
    print(
        "Confidence family hit: "
        f"optimizer={metrics['confidence_family_hit_rate']:.3f} "
        f"best-fixed={metrics['best_fixed_confidence_hit_rate']:.3f}"
    )
    print(
        "Diagnostic regret: "
        f"mean={metrics['mean_regret_percent']:.3f}% "
        f"P95={metrics['p95_regret_percent']:.3f}% "
        f"max={metrics['max_regret_percent']:.3f}%"
    )
    print(f"Result: {output / 'evaluation.json'}")


if __name__ == "__main__":
    main()
