"""Run the frozen TrustAero logical-approval scalability benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.validator_scalability import (
    load_validator_scalability_config,
    run_validator_scalability,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/validator_control_plane_scalability_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / args.config
    output = run_validator_scalability(
        load_validator_scalability_config(config_path), root, config_path
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(summary["status"])
    for row in summary["results"]:
        print(
            f"{row['case_id']}: median={row['median_latency_ms']:.4f} ms "
            f"p95={row['p95_latency_ms']:.4f} ms"
        )
    print(output.resolve())


if __name__ == "__main__":
    main()
