"""Run or resume the frozen V5 connection-isolated pairwise resolution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v5_pairwise_resolution import (
    load_pairwise_resolution_config,
    run_optimizer_v5_pairwise_resolution,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/optimizer_v5_pairwise_resolution_v1.json"
    config = load_pairwise_resolution_config(config_path)
    resume = args.resume
    if resume == "latest":
        latest = _load_latest(root / config.results_dir / "latest_run.json")
        resume = str(latest["run_id"])
    output = run_optimizer_v5_pairwise_resolution(
        config,
        project_root=root,
        resume_run_id=resume,
        show_progress=args.progress,
    )
    result = json.loads((output / "merged_inference.json").read_text(encoding="utf-8"))
    print(f"Pairwise resolution completed: {output}")
    print(
        f"Status: {result['status']}; model-eligible units: {result['model_eligible_unit_count']}"
    )


def _load_latest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


if __name__ == "__main__":
    main()
