"""Tests for grouped Optimizer V4 model-development primitives."""

from __future__ import annotations

from trustaero.experiments.optimizer_v4_model_development import _metrics, _regret
from trustaero.experiments.real_optimizer_transfer import EARLY_CANDIDATE, LATE_CANDIDATE


def test_v4_model_regret_uses_paired_ratio() -> None:
    assert _regret(1.5, EARLY_CANDIDATE) == 50.0
    assert _regret(1.5, LATE_CANDIDATE) == 0.0
    assert _regret(0.5, EARLY_CANDIDATE) == 0.0
    assert _regret(0.5, LATE_CANDIDATE) == 100.0


def test_v4_metrics_exclude_unstable_from_strong_claims() -> None:
    rows = [
        {
            "stable": True,
            "scheme": {
                "regret_percent": 0.0,
                "top1": True,
                "direct": True,
                "illegal": False,
            },
        },
        {
            "stable": False,
            "scheme": {
                "regret_percent": 100.0,
                "top1": False,
                "direct": False,
                "illegal": False,
            },
        },
    ]
    metrics = _metrics(rows, "scheme")
    assert metrics["evaluated_stable_family_count"] == 1
    assert metrics["mean_regret_percent"] == 0.0
    assert metrics["top1_selection_rate"] == 1.0
