"""Analyze a completed Execution-Aware calibration run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.execution_aware_calibration import (
    analyze_execution_aware_calibration,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.is_absolute():
            run_dir = root / run_dir
    else:
        latest = json.loads(
            (root / "results/execution_aware_calibration_v1/latest_run.json").read_text(
                encoding="utf-8"
            )
        )
        run_dir = root / "results/execution_aware_calibration_v1" / str(latest["run_id"])

    def progress(done: int, total: int, scenario_id: str) -> None:
        print(
            f"[EA calibration {done:02d}/{total:02d}] held out {scenario_id}",
            flush=True,
        )

    result = analyze_execution_aware_calibration(
        run_dir,
        progress_callback=progress,
    )
    validation = result["grouped_validation"]
    print(f"Status: {result['status']}", flush=True)
    print(
        "Oracle hit={:.3f} mean regret={:.3f}% P95={:.3f}% max={:.3f}%".format(
            validation["oracle_set_hit_rate"],
            validation["mean_regret_percent"],
            validation["p95_regret_percent"],
            validation["maximum_regret_percent"],
        ),
        flush=True,
    )
    print(f"Result: {run_dir / 'execution_aware_calibration.json'}", flush=True)


if __name__ == "__main__":
    main()
