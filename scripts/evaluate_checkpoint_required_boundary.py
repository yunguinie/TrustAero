"""Evaluate whether a required-checkpoint boundary needs multiple factors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.checkpoint_boundary_admission import (
    load_checkpoint_boundary_admission_config,
    run_checkpoint_boundary_admission,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", default="latest")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/checkpoint_required_boundary_evaluation_v1.json"
    config = load_checkpoint_boundary_admission_config(config_path)
    run_id = args.run_id
    if run_id == "latest":
        latest = root / config.measurement_results_dir / "latest_run.json"
        run_id = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    output = run_checkpoint_boundary_admission(
        config,
        project_root=root,
        measurement_run_id=run_id,
        config_path=config_path,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    print(f"Status: {result['status']}")
    print(f"Winners: {result['winner_counts']}")
    print(
        "Best q threshold: "
        f"{result['best_global_query_threshold']:.3f} "
        f"accuracy={result['best_global_query_threshold_accuracy']:.3f}"
    )
    print(
        "Cross-factor interaction q levels: "
        f"{result['query_levels_with_cross_factor_winner_interaction']}"
    )
    print(f"Result: {output / 'evaluation.json'}")


if __name__ == "__main__":
    main()
