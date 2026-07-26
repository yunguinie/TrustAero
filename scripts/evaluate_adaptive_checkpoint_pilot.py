"""Evaluate adaptive pilot decisions against the retained full-size oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.adaptive_checkpoint_pilot import (
    evaluate_adaptive_checkpoint_pilot,
    load_adaptive_pilot_config,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pilot_run", help="Pilot run path or 'latest'.")
    parser.add_argument(
        "--config",
        default="experiments/configs/adaptive_checkpoint_pilot_development_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_adaptive_pilot_config(root / args.config)
    pilot_run = args.pilot_run
    if pilot_run == "latest":
        latest = json.loads(
            (root / config.results_dir / "latest_run.json").read_text(encoding="utf-8")
        )
        pilot_run = str(root / config.results_dir / str(latest["run_id"]))
    output = evaluate_adaptive_checkpoint_pilot(
        config,
        pilot_run_dir=Path(pilot_run),
        project_root=root,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    adaptive = result["adaptive_metrics"]
    threshold = result["threshold_metrics"]
    print(f"Status: {result['status']}")
    print(
        "Adaptive: "
        f"hit={adaptive['confidence_family_hit_rate']:.3f} "
        f"mean={adaptive['mean_regret_percent']:.3f}% "
        f"P95={adaptive['p95_regret_percent']:.3f}% "
        f"max={adaptive['max_regret_percent']:.3f}%"
    )
    print(
        "Threshold: "
        f"hit={threshold['confidence_family_hit_rate']:.3f} "
        f"mean={threshold['mean_regret_percent']:.3f}% "
        f"P95={threshold['p95_regret_percent']:.3f}% "
        f"max={threshold['max_regret_percent']:.3f}%"
    )
    print(
        f"Conclusive pilots={result['conclusive_pilot_rate']:.3f} "
        f"amortized-speedup@{result['amortization_reuse_count']}="
        f"{result['amortized_speedup_vs_threshold']:.3f}x"
    )
    print(f"Result: {output / 'evaluation.json'}")


if __name__ == "__main__":
    main()
