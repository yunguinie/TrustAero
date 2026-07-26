"""Run the 100K TrustAero-governed BTS and NYC execution smoke."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    run_governed_real_data_smoke,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    print("Running TrustAero governed real-data smoke (no performance timings)...")
    try:
        payload = run_governed_real_data_smoke(PROJECT_ROOT)
    except GovernedRealDataSmokeError as exc:
        print(f"ERROR: {exc}")
        return 2

    for case in payload["governed_cases"]:
        print(
            f"PASS {case['case_id']}: validation={case['validation_status']}, "
            f"rows={case['row_count']:,}, certificate={case['certificate_status']}, "
            f"lineage_sources={case['lineage_source_count']}, "
            f"raw_sensitive_exposure={case['raw_sensitive_exposure_rows']}"
        )
    for case in payload["negative_cases"]:
        print(
            f"PASS {case['case_id']}: {case['actual_status']} with {case['expected_reason_code']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
