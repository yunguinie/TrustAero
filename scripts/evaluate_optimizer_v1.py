"""Evaluate the explainable Mask Optimizer V1 on a completed Phase 2E run."""

from __future__ import annotations

import argparse

from trustaero.experiments.optimizer_v1 import evaluate_mask_optimizer_v1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Completed Phase 2E/2F run directory.")
    parser.add_argument(
        "--evaluation-label",
        choices=("calibration", "held_out"),
        required=True,
        help="Declare whether this workload tuned V1 or was frozen as unseen data.",
    )
    parser.add_argument(
        "--max-raw-exposure-rows",
        type=int,
        help="Optional hard governance limit; zero requires early Mask.",
    )
    args = parser.parse_args()
    print(
        evaluate_mask_optimizer_v1(
            args.run_dir,
            evaluation_label=args.evaluation_label,
            max_raw_exposure_rows=args.max_raw_exposure_rows,
        )
    )


if __name__ == "__main__":
    main()
