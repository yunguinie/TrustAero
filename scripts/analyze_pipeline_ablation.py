"""Analyze a compact Phase 2M run under frozen governance policies."""

from __future__ import annotations

import argparse

from trustaero.experiments.pipeline_ablation_analysis import (
    analyze_compact_pipeline_ablation,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Completed compact Phase 2M run directory.")
    parser.add_argument("--output-dir", required=True, help="Derived analysis directory.")
    parser.add_argument("--tie-threshold", type=float, default=0.03)
    parser.add_argument("--required-seed-agreement", type=float, default=0.8)
    args = parser.parse_args()
    print("[phase2m-analysis 1/3] validating complete source run", flush=True)
    output = analyze_compact_pipeline_ablation(
        args.run_dir,
        args.output_dir,
        tie_threshold_fraction=args.tie_threshold,
        required_seed_agreement_fraction=args.required_seed_agreement,
    )
    print("[phase2m-analysis 2/3] legal optima computed for three policies", flush=True)
    print(f"[phase2m-analysis 3/3] artifacts written to {output}", flush=True)


if __name__ == "__main__":
    main()
