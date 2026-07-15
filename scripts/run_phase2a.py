"""Run Phase 2A controlled-data and physical-plan observation experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.phase2a import Phase2AConfig, run_phase2a
from trustaero.experiments.synthetic import SyntheticDataConfig


def load_phase2_config(path: str) -> Phase2AConfig:
    """Load the shared Phase 2A/2B configuration format."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    workloads = tuple(SyntheticDataConfig(**item) for item in payload["workloads"])
    return Phase2AConfig(
        results_dir=payload["results_dir"],
        workloads=workloads,
        warmup_runs=int(payload.get("warmup_runs", 2)),
        measured_runs=int(payload.get("measured_runs", 10)),
        duckdb_threads=int(payload.get("duckdb_threads", 4)),
        duckdb_memory_limit_mb=int(payload.get("duckdb_memory_limit_mb", 4096)),
        materialization_targets=tuple(payload.get("materialization_targets", ["op-event-project"])),
        source_lineage=bool(payload.get("source_lineage", False)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/phase2a.json",
        help="Phase 2A JSON configuration file.",
    )
    args = parser.parse_args()
    output_dir = run_phase2a(load_phase2_config(args.config))
    print(output_dir)


if __name__ == "__main__":
    main()
