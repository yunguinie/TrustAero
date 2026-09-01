"""Run the clean-source exact TPC-H Q3/Q10 semantic gate."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.tpch_query_coverage_v2 import (
    run_tpch_query_coverage_v2_smoke,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    print("[TPC-H V2 1/3] validate Q3/Q10 and generate legal candidates", flush=True)
    print("[TPC-H V2 2/3] compare candidates with official SF1 results", flush=True)
    result = run_tpch_query_coverage_v2_smoke(root)
    print("[TPC-H V2 3/3] verify plan diversity and source-lineage certificates", flush=True)
    print(f"Status: {result['status']}", flush=True)
    print(
        "Exact support: "
        f"{result['exact_support_count_after_smoke']}/"
        f"{result['official_query_denominator']} "
        f"({', '.join(result['exact_support_after_smoke'])})",
        flush=True,
    )
    print(f"Result: {root / 'results/tpch_query_coverage_v2/result.json'}", flush=True)


if __name__ == "__main__":
    main()
