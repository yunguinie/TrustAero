"""Run the frozen Phase 2L paired DuckDB operator attribution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.pipeline_attribution import (
    analyze_pipeline_operator_attribution,
)


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Phase 2L config must contain a JSON object")
    return cast(dict[str, Any], payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Frozen Phase 2L JSON config.")
    args = parser.parse_args()
    config = _load_config(Path(args.config).resolve())
    print("[phase2l 1/3] validating paired source artifacts", flush=True)
    output = analyze_pipeline_operator_attribution(
        [str(value) for value in config["source_run_dirs"]],
        str(config["output_dir"]),
        tie_threshold_fraction=float(config["tie_threshold_fraction"]),
        required_family_agreement_fraction=float(
            config["required_family_agreement_fraction"]
        ),
        minimum_sign_agreement=float(config["minimum_sign_agreement"]),
        minimum_absolute_spearman=float(config["minimum_absolute_spearman"]),
        minimum_dominant_family_fraction=float(
            config["minimum_dominant_family_fraction"]
        ),
    )
    print("[phase2l 2/3] paired operator roles and families analyzed", flush=True)
    print(f"[phase2l 3/3] artifacts written to {output}", flush=True)


if __name__ == "__main__":
    main()
