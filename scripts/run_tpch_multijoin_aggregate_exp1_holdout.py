"""Run the frozen one-shot SF10 Experiment 1 holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.tpch_multijoin_aggregate_exp1_holdout import run_holdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        default="experiments/frozen/tpch_multijoin_aggregate_exp1_sf10_holdout_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol = Path(args.protocol)
    if not protocol.is_absolute():
        protocol = root / protocol
    output = run_holdout(protocol, root)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {summary['status']}")
    print(f"Planner quality: {summary['planner_quality']}")
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
