"""Audit paired stability for the latest expanded January V4 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_calibration_audit import (
    write_optimizer_v4_calibration_audit,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="latest")
    args = parser.parse_args()
    results = root / "results/optimizer_v4_calibration_development"
    run_id = args.run_id
    if run_id == "latest":
        run_id = str(
            json.loads((results / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
        )
    output = write_optimizer_v4_calibration_audit(results / run_id)
    payload = json.loads(output.read_text(encoding="utf-8"))
    print(f"Audit: {output}")
    print(
        "V4 paired audit: {status}; stable={stable}/{families}; early={early}; "
        "late={late}; unstable={unstable}".format(
            status=payload["status"],
            stable=payload["stable_family_count"],
            families=payload["family_count"],
            early=payload["stable_early_preferred_count"],
            late=payload["stable_late_preferred_count"],
            unstable=payload["unstable_or_tie_family_count"],
        )
    )


if __name__ == "__main__":
    main()
