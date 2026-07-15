"""Summarize one or more Phase 1 DuckDB execution result runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.reporting import summarize_phase1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        default="results/phase1",
        help="Directory containing Phase 1 run subdirectories.",
    )
    parser.add_argument(
        "--output-dir",
        default="results/phase1_summary",
        help="Directory where summary CSV/JSON files should be written.",
    )
    args = parser.parse_args()

    summaries = summarize_phase1(Path(args.results_dir), Path(args.output_dir))
    print(f"Summarized {len(summaries)} Phase 1 run(s) into {args.output_dir}")


if __name__ == "__main__":
    main()
