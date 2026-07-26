"""Evaluate the frozen Lineage checkpoint model on the untouched holdout."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from trustaero.experiments.lineage_checkpoint_holdout import (
    evaluate_lineage_checkpoint_holdout,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    parser.add_argument(
        "--model",
        default="experiments/frozen/models/lineage_checkpoint_cost_model_v1_20260726.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    results_root = root / "results/lineage_checkpoint_holdout_v1"
    run_id = args.run
    if run_id == "latest":
        run_id = str(
            json.loads((results_root / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
        )
    run_dir = results_root / run_id
    output_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / "results/lineage_checkpoint_holdout_evaluation_v1" / output_id
    model_path = Path(args.model)
    if not model_path.is_absolute():
        model_path = root / model_path
    result = evaluate_lineage_checkpoint_holdout(
        run_dir,
        model_path=model_path,
        output_dir=output_dir,
    )
    model = result["model"]
    threshold = result["threshold_baseline"]
    print(f"Status: {result['status']}")
    print(
        "Model: "
        f"hit={model['oracle_set_hit_rate']:.3f} "
        f"mean={model['mean_regret_percent']:.3f}% "
        f"P95={model['p95_regret_percent']:.3f}% "
        f"max={model['maximum_regret_percent']:.3f}%"
    )
    print(
        "Threshold: "
        f"hit={threshold['oracle_set_hit_rate']:.3f} "
        f"mean={threshold['mean_regret_percent']:.3f}% "
        f"P95={threshold['p95_regret_percent']:.3f}%"
    )
    print(f"Result: {(output_dir / 'evaluation.json').resolve()}")


if __name__ == "__main__":
    main()
