"""Fit and grouped-cross-validate Mask Optimizer V2 on development runs."""

from __future__ import annotations

import argparse

from trustaero.experiments.optimizer_v2 import develop_mask_optimizer_v2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", help="Completed Phase 2E/F source runs.")
    parser.add_argument("--output-dir", required=True, help="Directory for derived V2 artifacts.")
    parser.add_argument(
        "--ridge-lambda",
        type=float,
        default=0.01,
        help="Frozen L2 coefficient for standardized non-intercept features.",
    )
    args = parser.parse_args()
    print(
        develop_mask_optimizer_v2(
            args.run_dirs,
            args.output_dir,
            ridge_lambda=args.ridge_lambda,
        )
    )


if __name__ == "__main__":
    main()
