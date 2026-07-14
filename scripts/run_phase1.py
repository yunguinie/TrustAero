"""Run the Phase 1 minimal DuckDB execution experiment.

Phase 1 is a smoke-level real execution experiment. It verifies that a
validated plan can be compiled, executed in DuckDB, digested, and checked
against a governed execution certificate. It is not yet a DBMS performance
benchmark.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.models import Phase1Config
from trustaero.experiments.phase1 import run_phase1


def _load_config(path: str) -> Phase1Config:
    """Load a Phase 1 JSON config file relative to the current working directory."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Phase1Config(
        results_dir=payload["results_dir"],
        warmup_runs=int(payload.get("warmup_runs", 3)),
        measured_runs=int(payload.get("measured_runs", 10)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        help="Optional JSON config file. Explicit CLI flags override this file.",
    )
    parser.add_argument(
        "--results-dir",
        default=None,
        help="Directory for run outputs relative to the repository root.",
    )
    parser.add_argument("--warmup-runs", type=int, default=None)
    parser.add_argument("--measured-runs", type=int, default=None)
    args = parser.parse_args()

    config = (
        _load_config(args.config) if args.config else Phase1Config(results_dir="results/phase1")
    )
    if args.results_dir is not None:
        config = Phase1Config(
            results_dir=args.results_dir,
            warmup_runs=config.warmup_runs,
            measured_runs=config.measured_runs,
        )
    if args.warmup_runs is not None:
        config = Phase1Config(
            results_dir=config.results_dir,
            warmup_runs=args.warmup_runs,
            measured_runs=config.measured_runs,
        )
    if args.measured_runs is not None:
        config = Phase1Config(
            results_dir=config.results_dir,
            warmup_runs=config.warmup_runs,
            measured_runs=args.measured_runs,
        )

    output_dir = run_phase1(config)
    print(output_dir)


if __name__ == "__main__":
    main()
