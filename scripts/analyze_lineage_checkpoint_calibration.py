"""Fit and group-validate the interpretable Lineage checkpoint cost model."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from trustaero.experiments.lineage_checkpoint_calibration import (
    analyze_lineage_checkpoint_calibration,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_dir",
        nargs="?",
        default="results/lineage_checkpoint_admission_v1/20260726T021127337754Z",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / "results/lineage_checkpoint_cost_calibration_v1" / run_id

    def progress(done: int, total: int, label: str) -> None:
        print(f"[Lineage-Cost {done:02d}/{total:02d}] held out {label}", flush=True)

    result = analyze_lineage_checkpoint_calibration(
        run_dir,
        output_dir,
        progress=progress,
    )
    validation = result["grouped_validation"]
    threshold = result["threshold_baseline"]
    print(f"Status: {result['status']}")
    print(
        "Model: "
        f"hit={validation['oracle_set_hit_rate']:.3f} "
        f"mean={validation['mean_regret_percent']:.3f}% "
        f"P95={validation['p95_regret_percent']:.3f}% "
        f"max={validation['maximum_regret_percent']:.3f}%"
    )
    print(
        "Threshold: "
        f"hit={threshold['oracle_set_hit_rate']:.3f} "
        f"mean={threshold['mean_regret_percent']:.3f}% "
        f"P95={threshold['p95_regret_percent']:.3f}%"
    )
    print(f"Result: {(output_dir / 'calibration.json').resolve()}")


if __name__ == "__main__":
    main()
