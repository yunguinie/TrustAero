"""Run the frozen paired thinking/non-thinking real-Agent plan experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_agent_plan_coverage import (
    run_real_agent_plan_coverage,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/real_agent_plan_generation_v1.json",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        help="Resume the latest run, or provide an explicit run ID.",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = run_real_agent_plan_coverage(
        root,
        config_path=root / args.config,
        progress=args.progress,
        resume_run_id=args.resume,
    )
    result = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {result['status']}")
    print(
        "Coverage: "
        f"api={result['api_success_rate']:.3f} "
        f"parse={result['strict_json_parse_rate']:.3f} "
        f"expected-family={result['expected_outcome_family_rate']:.3f} "
        f"unsafe={result['unauthorized_unsafe_count']}"
    )
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
