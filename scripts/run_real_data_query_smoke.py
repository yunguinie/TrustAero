"""Verify equivalent results and distinct DuckDB plans on real-data slices."""

from __future__ import annotations

from pathlib import Path

from trustaero.data.smoke import RealDataSmokeError, run_real_data_query_smoke

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"


def main() -> int:
    print("Running correctness-only real-data smoke (no paper timing claims)...")
    try:
        payload = run_real_data_query_smoke(DATA_ROOT)
    except RealDataSmokeError as exc:
        print(f"ERROR: {exc}")
        return 2

    for case in payload["cases"]:
        selectivity = case["filtered_rows"] / case["input_rows"]
        print(
            f"PASS {case['case_id']}: input={case['input_rows']:,}, "
            f"filtered={case['filtered_rows']:,} ({selectivity:.2%}), "
            f"joined={case['joined_rows']:,}, results_equal={case['results_equal']}, "
            f"plans_distinct={case['plans_distinct']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
