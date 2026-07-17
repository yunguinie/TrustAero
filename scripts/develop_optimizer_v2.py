"""Fit and grouped-cross-validate Mask Optimizer V2 on development runs."""

from __future__ import annotations

import argparse

from trustaero.experiments.optimizer_v2 import develop_mask_optimizer_v2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", help="Completed Phase 2E/F source runs.")
    parser.add_argument(
        "--output-dir", required=True, help="Directory for derived V2 artifacts."
    )
    parser.add_argument(
        "--ridge-lambda",
        type=float,
        default=0.01,
        help="Frozen L2 coefficient for standardized non-intercept features.",
    )
    parser.add_argument(
        "--residual-ridge-lambda",
        type=float,
        default=0.1,
        help="Frozen L2 coefficient for the regret-aware residual model.",
    )
    parser.add_argument(
        "--regret-weight-cap",
        type=float,
        default=10.0,
        help="Cap for continuous wrong-choice regret weighting.",
    )
    parser.add_argument(
        "--confidence-multiplier",
        type=float,
        default=1.0,
        help="Residual-RMSE margin required to overturn the base score.",
    )
    parser.add_argument(
        "--neighbor-group-count",
        type=int,
        default=3,
        help="Frozen number of nearest scenario families used by the local guard.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print one progress line after each completed outer guard fold.",
    )
    args = parser.parse_args()

    def report_guard_progress(completed: int, total: int, group: str) -> None:
        print(f"[local-guard {completed}/{total}] completed {group}", flush=True)

    print(
        develop_mask_optimizer_v2(
            args.run_dirs,
            args.output_dir,
            ridge_lambda=args.ridge_lambda,
            residual_ridge_lambda=args.residual_ridge_lambda,
            regret_weight_cap=args.regret_weight_cap,
            confidence_multiplier=args.confidence_multiplier,
            neighbor_group_count=args.neighbor_group_count,
            guard_progress_callback=report_guard_progress if args.progress else None,
        )
    )


if __name__ == "__main__":
    main()
