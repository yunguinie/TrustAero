"""Run frozen Experiment 2: hard legality versus soft penalties."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.hard_vs_soft_legality_exp2 import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        default="experiments/frozen/hard_vs_soft_legality_exp2_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol = Path(args.protocol)
    if not protocol.is_absolute():
        protocol = root / protocol
    output = run_experiment(protocol, root)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {summary['status']}")
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
