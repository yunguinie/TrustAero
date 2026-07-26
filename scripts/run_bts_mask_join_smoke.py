"""Run the BTS early/late Mask placement semantic smoke without timing."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.bts_mask_join import run_bts_mask_join_smoke


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    print("Running BTS Mask/Join placement semantic smoke (no timing)...")
    payload = run_bts_mask_join_smoke(root)
    print(
        "PASS: candidates={candidates}, distinct plans={plans}, output rows={rows}, "
        "strict policy forces early Mask={strict}".format(
            candidates=payload["candidate_count"],
            plans=payload["distinct_duckdb_plan_count"],
            rows=payload["candidates"][0]["output_row_count"],
            strict=payload["strict_policy_forces_early_mask"],
        )
    )


if __name__ == "__main__":
    main()
