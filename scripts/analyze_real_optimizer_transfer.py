"""Write a paired stability audit for a completed real-data transfer run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_data_governed import _atomic_json
from trustaero.experiments.real_optimizer_transfer import audit_real_optimizer_transfer


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        default="latest",
        help="Run ID or 'latest' under results/real_optimizer_transfer_development.",
    )
    args = parser.parse_args()
    results_root = root / "results/real_optimizer_transfer_development"
    run_id = args.run
    if run_id == "latest":
        run_id = str(
            json.loads((results_root / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
        )
    run_dir = results_root / run_id
    audit = audit_real_optimizer_transfer(run_dir)
    _atomic_json(run_dir / "paired_stability_audit.json", audit)
    print(f"Audit: {run_dir / 'paired_stability_audit.json'}")
    print(f"Status: {audit['status']}")
    print(
        "Stable families: {stable}/{total}; early={early}, late={late}; corrected "
        "P95 regret={p95:.3f}%".format(
            stable=audit["stable_family_count"],
            total=audit["family_count"],
            early=audit["stable_early_preferred_count"],
            late=audit["stable_late_preferred_count"],
            p95=audit["corrected_optimizer_v3_metrics"]["p95_regret_percent_nearest_rank"],
        )
    )


if __name__ == "__main__":
    main()
