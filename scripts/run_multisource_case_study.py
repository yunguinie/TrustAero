"""Run the frozen four-source TrustAero end-to-end semantic case study."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.multisource_case_study import (
    run_multisource_case_study,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print the seven bounded semantic-validation stages.",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Refuse publication evidence when the Git worktree is dirty.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = run_multisource_case_study(
        root,
        progress=args.progress,
        require_clean=args.require_clean,
    )
    print("Status: PASS_MULTISOURCE_CASE_STUDY_END_TO_END")
    print(f"Result: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
