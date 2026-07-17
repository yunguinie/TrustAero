"""Fit and evaluate the frozen mechanism-based Mask optimizer formula."""

from __future__ import annotations

import argparse

from trustaero.experiments.mechanism_optimizer import develop_mechanism_mask_optimizer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pilot-run-dir",
        required=True,
        help="Completed hash and materialization mechanism pilot run.",
    )
    parser.add_argument(
        "--join-run-dir",
        required=True,
        help="Completed repeated HASH_JOIN operator calibration run.",
    )
    parser.add_argument(
        "--workload-run-dirs",
        nargs="+",
        required=True,
        help="Completed end-to-end development runs used only for evaluation.",
    )
    parser.add_argument(
        "--frozen-predictions",
        required=True,
        help="Frozen V1/V2/residual/guard comparison predictions CSV.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--ridge-lambda",
        type=float,
        default=0.01,
        help="Frozen non-negative component-fit ridge coefficient.",
    )
    args = parser.parse_args()
    print("[1/3] Loading and fitting independent mechanism costs...", flush=True)
    output = develop_mechanism_mask_optimizer(
        args.pilot_run_dir,
        args.join_run_dir,
        args.workload_run_dirs,
        args.frozen_predictions,
        args.output_dir,
        ridge_lambda=args.ridge_lambda,
    )
    print("[2/3] Development comparisons and hard-constraint audits complete.", flush=True)
    print(f"[3/3] Results written to {output}", flush=True)


if __name__ == "__main__":
    main()
