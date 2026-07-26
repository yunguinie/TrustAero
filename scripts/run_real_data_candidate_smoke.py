"""Run approved multi-candidate semantics over 100K BTS and NYC slices."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.real_data_candidates import run_real_data_candidate_smoke


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    print("Running approved real-data candidate smoke (no performance timings)...")
    payload = run_real_data_candidate_smoke(root)
    for workload in payload["workloads"]:
        print(
            "PASS {name}: candidates={candidates}, distinct DuckDB plans={plans}, "
            "strict raw-boundary check={strict}".format(
                name=workload["workload"],
                candidates=workload["candidate_count"],
                plans=workload["distinct_duckdb_plan_count"],
                strict=workload["strict_profile_rejected_raw_boundary"],
            )
        )


if __name__ == "__main__":
    main()
