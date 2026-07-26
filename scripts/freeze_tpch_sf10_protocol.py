"""Bind verified SF10 semantic artifacts into deterministic formal configs."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.tpch_audit import TpchAuditError
from trustaero.experiments.tpch_sf10_protocol import freeze_tpch_sf10_protocol


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        result = freeze_tpch_sf10_protocol(root)
    except TpchAuditError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    print("Review and commit the two generated configs before formal timing.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
