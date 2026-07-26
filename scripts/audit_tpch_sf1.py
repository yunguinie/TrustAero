"""Run the complete TPC-H SF1 execution and IR-support audit with progress."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trustaero.experiments.tpch_audit import audit_tpch_sf1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    def progress(current: int, total: int, message: str) -> None:
        percent = current / total * 100
        print(f"[{current:02d}/{total:02d} {percent:5.1f}%] {message}", flush=True)

    result = audit_tpch_sf1(args.project_root, progress=progress)
    print(
        f"PASS: DuckDB {result['duckdb_execution_pass_count']}/22; "
        f"exact TrustAero IR v1 {result['ir_v1_supported_count']}/22",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
