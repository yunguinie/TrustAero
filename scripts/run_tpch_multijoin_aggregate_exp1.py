"""Run the formal SF1 development admission for Experiment 1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.tpch_multijoin_aggregate_exp1 import run_development


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        default="experiments/development/tpch_multijoin_aggregate_exp1_dev_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol = Path(args.protocol)
    if not protocol.is_absolute():
        protocol = root / protocol
    output = run_development(protocol, root)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {summary['status']}")
    print(f"Winners: {summary['singleton_winner_counts']}")
    print(f"Conclusive rate: {summary['conclusive_scenario_rate']:.3f}")
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
