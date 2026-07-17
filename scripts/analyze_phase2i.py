"""Analyze a complete Phase 2I fragment pilot with the frozen 3% tie band."""

from __future__ import annotations

import argparse

from trustaero.experiments.phase2i_analysis import analyze_phase2i_fragment_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Completed Phase 2I run directory.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--tie-threshold",
        type=float,
        default=0.03,
        help="Frozen practical-tie fraction; defaults to 0.03.",
    )
    parser.add_argument(
        "--required-seed-agreement",
        type=float,
        default=1.0,
        help="Fraction of a family's seeds required for a stable label.",
    )
    parser.add_argument(
        "--evaluation-label",
        default="phase2i_fragment_pilot_development_analysis",
    )
    args = parser.parse_args()
    output = analyze_phase2i_fragment_run(
        args.run_dir,
        args.output_dir,
        tie_threshold_fraction=args.tie_threshold,
        required_seed_agreement_fraction=args.required_seed_agreement,
        evaluation_label=args.evaluation_label,
    )
    print(output)


if __name__ == "__main__":
    main()
