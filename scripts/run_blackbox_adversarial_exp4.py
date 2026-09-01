"""Run the frozen black-box validation experiment."""

import argparse
import json
from pathlib import Path

from trustaero.experiments.blackbox_adversarial_exp4 import evaluate_frozen_cases, write_result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("artifact/results/rq1-blackbox/cases.json"),
        help="Frozen case corpus (default: committed artifact corpus).",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("examples/policies/research_policy.json"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("examples/catalogs/minimal_catalog.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/blackbox_adversarial_exp4_v1/replay_result.json"),
    )
    args = parser.parse_args()
    root = Path.cwd()
    result = evaluate_frozen_cases(
        root,
        cases_path=root / args.cases,
        policy_path=root / args.policy,
        catalog_path=root / args.catalog,
    )
    write_result(result, root / args.output)
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
