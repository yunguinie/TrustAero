"""Run frozen Experiment 3 candidate-space scalability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.candidate_space_scalability_exp3 import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        default="experiments/frozen/candidate_space_scalability_exp3_v1.json",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol = Path(args.protocol)
    if not protocol.is_absolute():
        protocol = root / protocol
    output = run_experiment(protocol, root)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {summary['status']}")
    print(f"Trials: {summary['planning_trial_count']}")
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
