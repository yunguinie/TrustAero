"""Analyze the latest approved real-data multi-candidate pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_data_candidate_analysis import (
    analyze_real_data_candidate_pilot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    else:
        results = root / "results/real_data_candidate_pilot"
        latest = json.loads((results / "latest_run.json").read_text(encoding="utf-8"))
        run_dir = results / str(latest["run_id"])
    result = analyze_real_data_candidate_pilot(run_dir)
    print(f"{result['status']}: {run_dir / 'report.md'}")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
