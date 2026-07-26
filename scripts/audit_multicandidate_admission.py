"""Run the source-bound multi-candidate optimizer admission audit."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.multicandidate_admission import (
    load_multicandidate_admission_config,
    run_multicandidate_admission,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/multicandidate_optimizer_admission_v1.json"
    config = load_multicandidate_admission_config(config_path)
    output = run_multicandidate_admission(
        config,
        project_root=root,
        config_path=config_path,
    )
    audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
    print(f"Status: {audit['status']}")
    print(
        "3+ candidate families="
        f"{len(audit['structural_three_candidate_families'])}; "
        "within-family diverse="
        f"{len(audit['three_candidate_families_with_internal_winner_diversity'])}"
    )
    print(f"Failed gates: {audit['failed_gates']}")
    print(f"Result: {output / 'audit.json'}")


if __name__ == "__main__":
    main()
