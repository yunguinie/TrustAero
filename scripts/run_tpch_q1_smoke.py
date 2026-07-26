"""Run the governed official TPC-H SF1 Q1 semantic smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.tpch_q1 import run_tpch_q1_semantic_smoke


def main() -> None:
    """Run Q1 from the repository root and print its auditable summary."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale-factor", type=int, choices=(1, 10), default=1)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(
        f"[TPC-H SF{args.scale_factor} Q1] validating and executing three governed candidates...",
        flush=True,
    )
    result = run_tpch_q1_semantic_smoke(root, scale_factor=args.scale_factor)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
