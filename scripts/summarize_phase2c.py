"""Create paired seed-level reports for one completed Phase 2C run."""

from __future__ import annotations

import argparse

from trustaero.experiments.phase2c_analysis import analyze_phase2c_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Completed Phase 2C run directory.")
    parser.add_argument(
        "--bootstrap-runs",
        type=int,
        default=2000,
        help="Number of paired seed bootstrap resamples (default: 2000).",
    )
    parser.add_argument(
        "--minimum-stable-seeds",
        type=int,
        default=5,
        help="Minimum independent seeds for a stable-reversal label (default: 5).",
    )
    args = parser.parse_args()
    print(
        analyze_phase2c_run(
            args.run_dir,
            bootstrap_runs=args.bootstrap_runs,
            minimum_stable_seeds=args.minimum_stable_seeds,
        )
    )


if __name__ == "__main__":
    main()
