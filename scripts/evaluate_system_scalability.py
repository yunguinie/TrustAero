"""Evaluate a frozen paired TrustAero system-scalability run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.system_scalability_evaluation import (
    evaluate_system_scalability,
    load_system_scalability_evaluation_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    parser.add_argument(
        "--config",
        default="experiments/configs/system_scalability_formal_evaluation_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / args.config
    config = load_system_scalability_evaluation_config(config_path)
    run_id = args.run
    if run_id == "latest":
        latest = root / config.measurement_results_dir / "latest_run.json"
        run_id = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    output = evaluate_system_scalability(
        config,
        project_root=root,
        measurement_run_dir=root / config.measurement_results_dir / run_id,
        config_path=config_path,
    )
    result = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    print(f"Status: {result['status']}")
    for unit in result["unit_findings"]:
        print(
            f"{unit['unit_id']}: overhead="
            f"{unit['median_complete_over_direct_overhead_percent']:.3f}% "
            f"CI=[{unit['paired_bootstrap_confidence_interval']['lower']:.3f}, "
            f"{unit['paired_bootstrap_confidence_interval']['upper']:.3f}]"
        )
    print(f"Result: {(output / 'evaluation.json').resolve()}")


if __name__ == "__main__":
    main()
