"""Run the frozen optimizer workload-sufficiency audit."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.optimizer_workload_sufficiency import (
    load_workload_sufficiency_config,
    run_workload_sufficiency_audit,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/optimizer_workload_sufficiency_v1.json"
    run_dir = run_workload_sufficiency_audit(
        load_workload_sufficiency_config(config_path),
        project_root=root,
        config_path=config_path,
    )
    payload = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    print(f"Run directory: {run_dir}")
    print(f"Status: {payload['status']}")
    print(
        "Fixed-match reversal strata: {reversals}; match-only top-1: {top1:.1%}; "
        "query templates: {templates}".format(
            reversals=payload["fixed_match_rate_reversal_strata"],
            top1=payload["match_rate_baseline_top1"],
            templates=len(payload["query_template_ids"]),
        )
    )


if __name__ == "__main__":
    main()
