"""Apply frozen integrity gates to a completed real-data pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_data_pilot_analysis import analyze_real_data_pilot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_dir",
        nargs="?",
        help="Run directory; defaults to results/real_data_pilot/latest_run.json.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    else:
        results = root / "results/real_data_pilot"
        latest = json.loads((results / "latest_run.json").read_text(encoding="utf-8"))
        run_dir = results / str(latest["run_id"])
    acceptance = analyze_real_data_pilot(run_dir)
    print(f"{acceptance['status']}: {run_dir / 'report.md'}")
    if acceptance["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
