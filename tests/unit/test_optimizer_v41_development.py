"""Tests for V4.1 deployed and direct-selector metrics."""

from __future__ import annotations

from trustaero.experiments.optimizer_v41_development import _deployed_metrics


def test_v41_deployed_metrics_use_only_stable_claim_rows() -> None:
    rows = [
        {"stable": True, "scheme": {"top1": True, "regret_percent": 0.0}},
        {"stable": False, "scheme": {"top1": False, "regret_percent": 100.0}},
    ]
    metrics = _deployed_metrics(rows, "scheme")
    assert metrics["stable_family_count"] == 1
    assert metrics["top1_selection_rate"] == 1.0
    assert metrics["mean_regret_percent"] == 0.0
