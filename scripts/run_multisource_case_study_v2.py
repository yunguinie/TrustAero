"""Run the frozen TrustAero four-source complete-system case study V2."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.multisource_case_study_v2 import (
    run_multisource_case_study_v2,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show the nine bounded stages; a normal run takes about 10-30 seconds.",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Reject publication evidence from a dirty Git worktree.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = run_multisource_case_study_v2(
        root,
        progress=args.progress,
        require_clean=args.require_clean,
    )
    print("Status: PASS_MULTISOURCE_CASE_STUDY_V2_COMPLETE_LOOP")
    print(f"Result: {output / 'summary.json'}")
    print(f"Report: {output / 'report.md'}")


if __name__ == "__main__":
    main()
