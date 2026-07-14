"""Run the Phase 0 repeatable semantic evaluation.

This script intentionally measures TrustAero validation only. It does not
execute SQL, call DuckDB, or benchmark a physical database engine.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.models import Phase0Config
from trustaero.experiments.runner import run_phase0


def _load_config(path: str) -> Phase0Config:
    """Load a Phase 0 JSON config file relative to the current working directory."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Phase0Config(
        cases_path=payload["cases_path"],
        results_dir=payload["results_dir"],
        warmup_runs=int(payload.get("warmup_runs", 5)),
        measured_runs=int(payload.get("measured_runs", 30)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        help="Optional JSON config file. Explicit CLI flags override this file.",
    )
    parser.add_argument(
        "--cases",
        default=None,
        help="CSV matrix of Phase 0 cases relative to the repository root.",
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
        _load_config(args.config)
        if args.config
        else Phase0Config(
            cases_path="experiments/cases/phase0_cases.csv",
            results_dir="results/phase0",
        )
    )
    if args.cases is not None:
        config = Phase0Config(
            cases_path=args.cases,
            results_dir=config.results_dir,
            warmup_runs=config.warmup_runs,
            measured_runs=config.measured_runs,
        )
    if args.results_dir is not None:
        config = Phase0Config(
            cases_path=config.cases_path,
            results_dir=args.results_dir,
            warmup_runs=config.warmup_runs,
            measured_runs=config.measured_runs,
        )
    if args.warmup_runs is not None:
        config = Phase0Config(
            cases_path=config.cases_path,
            results_dir=config.results_dir,
            warmup_runs=args.warmup_runs,
            measured_runs=config.measured_runs,
        )
    if args.measured_runs is not None:
        config = Phase0Config(
            cases_path=config.cases_path,
            results_dir=config.results_dir,
            warmup_runs=config.warmup_runs,
            measured_runs=args.measured_runs,
        )

    output_dir = run_phase0(config)
    print(output_dir)


if __name__ == "__main__":
    main()
