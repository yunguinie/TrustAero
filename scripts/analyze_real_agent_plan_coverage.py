"""Generate paper-facing aggregates for a completed real-Agent coverage run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_agent_plan_coverage import (
    analyze_real_agent_plan_coverage,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="latest")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    results = root / "results/real_agent_plan_coverage_v1"
    run_id = args.run
    if run_id == "latest":
        run_id = str(
            json.loads((results / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
        )
    target = analyze_real_agent_plan_coverage(results / run_id)
    payload = json.loads(target.read_text(encoding="utf-8"))
    print(f"Status: {payload['status']}")
    print(
        f"Cells={payload['cell_count']} "
        f"unsafe={payload['safety']['unauthorized_unsafe_count']} "
        f"unexpected={len(payload['unexpected_cells'])}"
    )
    print(f"Result: {target.resolve()}")


if __name__ == "__main__":
    main()
