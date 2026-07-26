"""Diagnose the latest failed Optimizer V4 development run."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_model_diagnosis import (
    write_optimizer_v4_model_diagnosis,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    results = root / "results/optimizer_v4_model_development"
    run_id = str(json.loads((results / "latest_run.json").read_text(encoding="utf-8"))["run_id"])
    output = write_optimizer_v4_model_diagnosis(results / run_id)
    payload = json.loads(output.read_text(encoding="utf-8"))
    print(f"Diagnosis: {output}")
    print(
        "V4 sign={correct}/{stable}; direct={direct}; fallback wrong={wrong}; "
        "status={status}".format(
            correct=payload["counterfactual_prediction_sign_correct_count"],
            stable=payload["stable_family_count"],
            direct=payload["direct_stable_decision_count"],
            wrong=payload["stable_fallback_wrong_count"],
            status=payload["status"],
        )
    )


if __name__ == "__main__":
    main()
