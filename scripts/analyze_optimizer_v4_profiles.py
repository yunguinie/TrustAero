"""Audit the latest V4 profiles against frozen paired timing labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_profile_analysis import (
    write_optimizer_v4_profile_analysis,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="latest")
    args = parser.parse_args()
    results_root = root / "results/optimizer_v4_profiles_development"
    run_id = args.run_id
    if run_id == "latest":
        latest = json.loads((results_root / "latest_run.json").read_text(encoding="utf-8"))
        run_id = str(latest["run_id"])
    output = write_optimizer_v4_profile_analysis(
        results_root / run_id,
        root / "results/real_optimizer_transfer_development/20260721T084854695495Z/"
        "paired_stability_audit.json",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    print(f"Analysis: {output}")
    print(
        "Profile audit: {status}; stable agreement={agree}/{stable}; "
        "disagreements={disagree}".format(
            status=payload["status"],
            agree=payload["profile_direction_agreement_on_stable_count"],
            stable=payload["stable_paired_family_count"],
            disagree=payload["profile_direction_disagreement_on_stable_count"],
        )
    )


if __name__ == "__main__":
    main()
