"""Run the BTS fact/airport/carrier semantic smoke without timing."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.bts_multijoin import run_bts_multijoin_smoke


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    print("Running BTS natural multi-Join semantic smoke (no timing)...")
    payload = run_bts_multijoin_smoke(root)
    print(
        "PASS: candidates={candidates}, distinct DuckDB plans={plans}, output rows={rows}".format(
            candidates=payload["candidate_count"],
            plans=payload["distinct_duckdb_plan_count"],
            rows=payload["candidates"][0]["output_row_count"],
        )
    )


if __name__ == "__main__":
    main()
