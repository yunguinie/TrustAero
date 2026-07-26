"""Tests for corrected real-data transfer aggregation and audit contracts."""

from __future__ import annotations

from trustaero.experiments.real_optimizer_transfer import (
    RealOptimizerTransferConfig,
    TransferGateThresholds,
    _summarize,
)


def _config() -> RealOptimizerTransferConfig:
    return RealOptimizerTransferConfig(
        protocol_name="test-transfer",
        scientific_label="test-only",
        results_dir="results/test-transfer",
        identifier_widths=(192,),
        target_match_rates=(0.25,),
        warmup_blocks=0,
        measured_blocks=2,
        duckdb_threads=1,
        duckdb_memory_limit_mb=512,
        order_seed=1,
        tie_threshold_fraction=0.03,
        require_clean_git=False,
        primary_model_path="primary.json",
        stability_models_path="stability.json",
        frozen_model_record="record.json",
        gate=TransferGateThresholds(0.0, 100.0, 100.0, 0.0),
        scientific_boundary="unit test",
    )


def test_transfer_p95_uses_nearest_rank_for_twelve_families() -> None:
    """At n=12, nearest-rank P95 is the twelfth (maximum) observation."""

    regrets = [0.0] * 6 + [36.0, 42.0, 43.0, 53.0, 55.0, 67.0]
    families = [
        {
            "status": "PASS",
            "timings": [{}, {}],
            "optimizer_v3": {
                "regret_percent": regret,
                "within_3_percent": regret <= 3.0,
                "direct_model_decision": True,
            },
        }
        for regret in regrets
    ]

    summary = _summarize(_config(), families)

    assert summary["optimizer_v3_metrics"]["p95_regret_percent"] == 67.0
