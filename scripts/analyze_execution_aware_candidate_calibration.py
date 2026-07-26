"""Analyze a completed run with candidate-specific analytic cost models."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.execution_aware_candidate_calibration import (
    analyze_candidate_specific_calibration,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir

    def progress(done: int, total: int, scenario_id: str) -> None:
        print(
            f"[Candidate-specific {done:02d}/{total:02d}] held out {scenario_id}",
            flush=True,
        )

    result = analyze_candidate_specific_calibration(run_dir, progress_callback=progress)
    validation = result["grouped_validation"]
    fallback = result["fixed_fallback_validation"]
    print(f"Status: {result['status']}", flush=True)
    print(
        "Candidate-specific: hit={:.3f} mean={:.3f}% P95={:.3f}% max={:.3f}% "
        "guard={:.3f} override={:.3f}".format(
            validation["oracle_set_hit_rate"],
            validation["mean_regret_percent"],
            validation["p95_regret_percent"],
            validation["maximum_regret_percent"],
            validation["guard_trigger_rate"],
            validation["model_override_rate"],
        ),
        flush=True,
    )
    print(
        "Fixed fallback: hit={:.3f} mean={:.3f}% P95={:.3f}% max={:.3f}%".format(
            fallback["oracle_set_hit_rate"],
            fallback["mean_regret_percent"],
            fallback["p95_regret_percent"],
            fallback["maximum_regret_percent"],
        ),
        flush=True,
    )
    print(
        f"Result: {run_dir / 'execution_aware_deployment_candidate_calibration.json'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
