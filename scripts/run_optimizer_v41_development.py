"""Run the frozen V4.1 complete-group sign-stability evaluation."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.optimizer_v41_development import (
    load_v41_development_config,
    run_optimizer_v41_development,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/optimizer_v41_development_v1.json"
    run_dir = run_optimizer_v41_development(
        load_v41_development_config(config_path),
        project_root=root,
        config_path=config_path,
    )
    payload = json.loads((run_dir / "cross_validation.json").read_text(encoding="utf-8"))
    direct = payload["direct_selector_metrics"]
    conservative = payload["deployed_metrics"]["v41_conservative_early"]
    print(f"Run directory: {run_dir}")
    print(
        "V4.1: {status}; direct precision={precision:.1%}; coverage={coverage:.1%}; "
        "uncertain capture={capture:.1%}".format(
            status=payload["status"],
            precision=direct["direct_precision"],
            coverage=direct["direct_coverage"],
            capture=payload["unstable_uncertainty_capture"],
        )
    )
    print(
        "Conservative early deployment: mean={mean:.3f}%; p95={p95:.3f}%; "
        "max={maximum:.3f}%".format(
            mean=conservative["mean_regret_percent"],
            p95=conservative["p95_regret_percent"],
            maximum=conservative["max_regret_percent"],
        )
    )


if __name__ == "__main__":
    main()
