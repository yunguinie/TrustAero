"""Tests for separating V4 ranking and fallback failures."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_model_diagnosis import (
    diagnose_optimizer_v4_model,
)


def test_v4_diagnosis_attributes_fallback_error(tmp_path: Path) -> None:
    payload = {
        "status": "FAIL_V4_DEVELOPMENT_GATE_RETAIN",
        "folds": [{"v4_uncertainty_threshold": 1.0}],
        "metrics": {
            "optimizer_v4": {"mean_regret_percent": 10.0},
            "match_rate_baseline": {"mean_regret_percent": 0.0},
        },
        "predictions": [
            {
                "family_id": "late-family",
                "stable": True,
                "actual_direction": "late_mask",
                "v4_prediction": 0.2,
                "v4_reason_code": "MASK_V4_UNCERTAIN_CONSERVATIVE_EARLY",
                "optimizer_v4": {"direct": False, "top1": False},
            },
            {
                "family_id": "early-family",
                "stable": True,
                "actual_direction": "early_mask_materialized",
                "v4_prediction": -0.2,
                "v4_reason_code": "MASK_V4_CONFIDENT_COST_RANKING",
                "optimizer_v4": {"direct": True, "top1": True},
            },
        ],
    }
    run = tmp_path / "run"
    run.mkdir()
    (run / "cross_validation.json").write_text(json.dumps(payload), encoding="utf-8")

    diagnosis = diagnose_optimizer_v4_model(run)

    assert diagnosis["counterfactual_prediction_sign_accuracy"] == 1.0
    assert diagnosis["direct_stable_correct_count"] == 1
    assert diagnosis["stable_fallback_wrong_count"] == 1
    assert (
        diagnosis["failure_categories"]["residual_uncertainty_calibration_failure"] == "CONFIRMED"
    )
