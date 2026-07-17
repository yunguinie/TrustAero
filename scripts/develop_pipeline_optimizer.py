"""Run the frozen Phase 2K pipeline-aware optimizer development evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.pipeline_optimizer import develop_pipeline_mask_optimizer


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Phase 2K config must contain a JSON object")
    return cast(dict[str, Any], payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Frozen Phase 2K JSON config.")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print completed family folds, elapsed time, and ETA.",
    )
    args = parser.parse_args()
    config = _load_config(Path(args.config).resolve())

    def report(completed: int, total: int, family_id: str, elapsed: float) -> None:
        eta = max(0.0, elapsed / completed * (total - completed))
        print(
            f"[pipeline-cv {completed}/{total}] {family_id} "
            f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    output = develop_pipeline_mask_optimizer(
        [str(value) for value in config["source_run_dirs"]],
        str(config["output_dir"]),
        tie_threshold_fraction=float(config["tie_threshold_fraction"]),
        ridge_lambda=float(config["ridge_lambda"]),
        uncertainty_multiplier=float(config["uncertainty_multiplier"]),
        minimum_direct_coverage=float(config["minimum_direct_coverage"]),
        progress_callback=report if args.progress else None,
    )
    print(output, flush=True)


if __name__ == "__main__":
    main()
