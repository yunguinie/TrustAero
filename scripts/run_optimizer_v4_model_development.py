"""Run frozen grouped development evaluation for Optimizer V4."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_model_development import (
    load_v4_model_development_config,
    run_optimizer_v4_model_development,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/optimizer_v4_model_development_v1.json",
    )
    args = parser.parse_args()
    config_path = root / args.config
    run_dir = run_optimizer_v4_model_development(
        load_v4_model_development_config(config_path),
        project_root=root,
        config_path=config_path,
    )
    payload = json.loads((run_dir / "cross_validation.json").read_text(encoding="utf-8"))
    v4 = payload["metrics"]["optimizer_v4"]
    simple = payload["metrics"]["match_rate_baseline"]
    print(f"Run directory: {run_dir}")
    print(
        "V4 development: {status}; within3={within:.1%}; mean={mean:.3f}%; "
        "p95={p95:.3f}%; max={maximum:.3f}%; direct={direct:.1%}".format(
            status=payload["status"],
            within=v4["within_3_percent_rate"],
            mean=v4["mean_regret_percent"],
            p95=v4["p95_regret_percent"],
            maximum=v4["max_regret_percent"],
            direct=v4["direct_coverage"],
        )
    )
    print(
        "Match-rate baseline: within3={within:.1%}; mean={mean:.3f}%; p95={p95:.3f}%".format(
            within=simple["within_3_percent_rate"],
            mean=simple["mean_regret_percent"],
            p95=simple["p95_regret_percent"],
        )
    )


if __name__ == "__main__":
    main()
