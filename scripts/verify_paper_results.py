"""Verify the immutable evidence selected for publication.

Run this before preparing tables or figures.  The command is read-only and
returns a non-zero exit code if any recorded result is missing or has changed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.reproducibility.paper_results import verify_paper_results_registry


def main() -> None:
    """Parse the registry path, verify every digest, and print a short report."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        default="experiments/frozen/paper_results_registry_v1_20260724.json",
        help="Repository-relative path to the publication result registry.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete machine-readable verification result.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    result = verify_paper_results_registry(root, Path(args.registry))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        failures = [check for check in result.checks if check.status != "PASS"]
        print(f"Status: {result.status}_PAPER_RESULTS_REGISTRY")
        print(f"Entries: {result.entry_count}; verified artifacts: {result.artifact_count}")
        for check in failures:
            print(f"{check.status}: {check.entry_id} -> {check.path}")
    if result.status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
