"""Run and analyze a frozen governed TPC-H Q1 timing protocol."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trustaero.experiments.tpch_q1_formal import (
    analyze_tpch_q1_formal,
    load_tpch_q1_formal_config,
    run_tpch_q1_formal,
)


def main() -> int:
    """Execute Q1 with optional live progress and return failure through the shell."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("experiments/configs/tpch_q1_utc_batched_v1.json"),
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--resume-run-id",
        help=(
            "Resume complete persisted blocks for diagnostics; final paper evidence "
            "still requires one uninterrupted process."
        ),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_tpch_q1_formal_config(root / args.config)
    run_dir = run_tpch_q1_formal(
        config,
        project_root=root,
        show_progress=args.progress,
        resume_run_id=args.resume_run_id,
    )
    acceptance = analyze_tpch_q1_formal(run_dir)
    print(f"run_dir={run_dir}", flush=True)
    print(f"acceptance={acceptance['status']}", flush=True)
    print(
        f"formal_paper_experiment_authorized={acceptance['formal_paper_experiment_authorized']}",
        flush=True,
    )
    if acceptance.get("claim_authorization_required"):
        print(
            f"authorized_claims={acceptance['authorized_claim_count']}/"
            f"{len(acceptance['paired_claims'])}",
            flush=True,
        )
    return 0 if acceptance["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
