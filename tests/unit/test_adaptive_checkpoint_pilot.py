from __future__ import annotations

import pytest

from trustaero.experiments.adaptive_checkpoint_pilot import (
    AdaptivePilotExperimentConfig,
    _paired_blocks,
)


def _config() -> AdaptivePilotExperimentConfig:
    return AdaptivePilotExperimentConfig(
        results_dir="results/test",
        source_measurement_run="results/source/run",
        source_measurement_config_path="experiments/configs/source.json",
        expected_source_summary_sha256="0" * 64,
        expected_source_measurements_sha256="1" * 64,
        expected_source_config_sha256="2" * 64,
        pilot_row_count=15_000,
        pilot_warmup_rounds=1,
        pilot_repetitions_per_permutation=5,
        duckdb_threads=1,
        duckdb_memory_limit_mb=4096,
        order_seed=1,
        practical_tie_fraction=0.03,
        confidence_level=0.95,
        bootstrap_draws=2_000,
        bootstrap_seed=2,
        fallback_query_selectivity_threshold=0.35,
        minimum_confidence_family_hit_improvement=0.03,
        minimum_mean_regret_reduction_percent=0.25,
        maximum_mean_regret_percent=1.0,
        maximum_p95_regret_percent=5.0,
        maximum_regret_percent=12.0,
        minimum_conclusive_pilot_rate=0.30,
        amortization_reuse_count=20,
        minimum_amortized_speedup_vs_threshold=1.0,
        require_clean_git=False,
    )


def test_pilot_budget_expands_to_ten_balanced_blocks() -> None:
    assert _config().measured_blocks_per_unit == 10


def test_paired_blocks_require_both_candidates() -> None:
    rows = [
        {
            "repeat_index": 0,
            "candidate_id": "policy_first_narrow_checkpoint",
            "latency_ms": 1.0,
        }
    ]
    with pytest.raises(ValueError, match="incomplete"):
        _paired_blocks(rows)


def test_pilot_scale_drift_is_rejected() -> None:
    values = {name: getattr(_config(), name) for name in _config().__dataclass_fields__}
    values["pilot_row_count"] = 20_000
    with pytest.raises(ValueError, match="15000"):
        AdaptivePilotExperimentConfig(**values)
