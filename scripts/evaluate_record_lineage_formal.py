"""Evaluate the frozen formal record-lineage scalability run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.record_lineage_evaluation import (
    evaluate_record_lineage_formal,
    load_record_lineage_evaluation_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    parser.add_argument(
        "--config",
        default="experiments/configs/record_lineage_formal_evaluation_v3.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / args.config
    config = load_record_lineage_evaluation_config(config_path)
    run_id = args.run
    if run_id == "latest":
        latest = root / config.measurement_results_dir / "latest_run.json"
        run_id = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    output = evaluate_record_lineage_formal(
        config,
        project_root=root,
        measurement_run_dir=root / config.measurement_results_dir / run_id,
        config_path=config_path,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    print(f"Status: {result['status']}")
    for unit in result["unit_findings"]:
        interval = unit["paired_bootstrap_confidence_interval"]
        print(
            f"n={unit['row_count']}: overhead="
            f"{unit['median_record_over_direct_overhead_percent']:.3f}% "
            f"CI=[{interval['lower']:.3f}, {interval['upper']:.3f}] "
            f"storage={unit['bytes_per_edge']:.3f}B/edge"
        )
    print(f"Result: {(output / 'evaluation.json').resolve()}")


if __name__ == "__main__":
    main()
