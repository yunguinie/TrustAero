"""Extract label-free January inputs for the Optimizer V4 contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_statistics import (
    run_optimizer_v4_statistics_preflight,
)
from trustaero.experiments.real_optimizer_transfer import (
    load_real_optimizer_transfer_config,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/real_optimizer_transfer_development_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    config = load_real_optimizer_transfer_config(root / args.config)
    run_dir = run_optimizer_v4_statistics_preflight(
        config, project_root=root, show_progress=args.progress
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"\nRun directory: {run_dir}")
    print(
        "V4 statistics preflight: {status}; families={families}; model_fitted={model}".format(
            status=summary["status"],
            families=summary["family_count"],
            model=summary["model_fitted"],
        )
    )


if __name__ == "__main__":
    main()
