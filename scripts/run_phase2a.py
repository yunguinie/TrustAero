"""Run Phase 2A controlled-data and physical-plan observation experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.phase2a import Phase2AConfig, run_phase2a
from trustaero.experiments.synthetic import SyntheticDataConfig


def _load_config(path: str) -> Phase2AConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    workloads = tuple(SyntheticDataConfig(**item) for item in payload["workloads"])
    return Phase2AConfig(
        results_dir=payload["results_dir"],
        workloads=workloads,
        warmup_runs=int(payload.get("warmup_runs", 2)),
        measured_runs=int(payload.get("measured_runs", 10)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/phase2a.json",
        help="Phase 2A JSON configuration file.",
    )
    args = parser.parse_args()
    output_dir = run_phase2a(_load_config(args.config))
    print(output_dir)


if __name__ == "__main__":
    main()
