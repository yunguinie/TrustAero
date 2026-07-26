"""Audit whether accepted real-data labels can train Optimizer V5."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.optimizer_v5_training_readiness import (
    load_training_readiness_config,
    run_optimizer_v5_training_readiness_audit,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/optimizer_v5_training_readiness_v1.json"
    config = load_training_readiness_config(config_path)
    output = run_optimizer_v5_training_readiness_audit(
        config,
        project_root=root,
        config_path=config_path,
    )
    audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
    print(f"Training-readiness audit completed: {output}")
    print(
        f"Status: {audit['status']}; units={audit['performance_unit_count']}; "
        f"unrestricted non-baseline winners="
        f"{audit['unrestricted_nonbaseline_winner_count']}"
    )


if __name__ == "__main__":
    main()
