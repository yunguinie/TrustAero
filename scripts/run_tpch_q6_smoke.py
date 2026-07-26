"""Run official TPC-H Q6 through the complete TrustAero semantic loop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trustaero.experiments.tpch_q6 import run_tpch_q6_semantic_smoke


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale-factor", type=int, choices=(1, 10), default=1)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(
        f"[1/4] verify SF{args.scale_factor} artifact and validate governed Q6",
        flush=True,
    )
    result = run_tpch_q6_semantic_smoke(root, scale_factor=args.scale_factor)
    print("[2/4] compare every candidate with official Q6", flush=True)
    print("[3/4] verify source lineage and execution certificates", flush=True)
    print(
        f"[4/4] PASS: {result['candidate_count']} candidates, "
        f"{result['distinct_duckdb_plan_count']} distinct DuckDB plans",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
