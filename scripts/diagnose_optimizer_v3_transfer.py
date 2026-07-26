"""Generate the frozen V3 real-pipeline root-cause diagnosis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_transfer_diagnosis import (
    diagnose_optimizer_v3_transfer,
)
from trustaero.experiments.real_data_governed import _atomic_json


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="latest")
    args = parser.parse_args()
    results = root / "results/real_optimizer_transfer_development"
    run_id = args.run
    if run_id == "latest":
        run_id = str(
            json.loads((results / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
        )
    run_dir = results / run_id
    report = diagnose_optimizer_v3_transfer(root, run_dir)
    output = run_dir / "v3_root_cause_diagnosis.json"
    _atomic_json(output, report)
    print(f"Diagnosis: {output}")
    print(f"Status: {report['status']}")
    print(
        "V3 final late={late}/{families}; primary correct on stable={correct}/{stable}".format(
            late=report["final_v3_late_selection_count"],
            families=report["family_count"],
            correct=report["primary_model_correct_direction_on_stable_families"],
            stable=report["stable_family_count"],
        )
    )


if __name__ == "__main__":
    main()
