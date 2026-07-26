"""Controls for the frozen three-candidate TPC-H Q6 timing protocol."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_governed import GovernedRealDataSmokeError
from trustaero.experiments.tpch_q6_formal import (
    TPCH_Q6_SF10_FORMAL_LABEL,
    TPCH_Q6_SF10_PAIRED_CI_LABEL,
    TpchQ6Timing,
    _completed_measurement_blocks,
    load_tpch_q6_formal_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_q6_formal_config_requires_complete_permutation_cycles() -> None:
    config = load_tpch_q6_formal_config(PROJECT_ROOT / "experiments/configs/tpch_q6_formal_v1.json")

    with pytest.raises(ValueError, match="at least 30"):
        replace(config, measured_blocks=24)
    with pytest.raises(ValueError, match="six candidate permutations"):
        replace(config, warmup_blocks=5)
    with pytest.raises(ValueError, match="scope"):
        replace(config, heldout_optimizer_evidence=True)


def test_q6_schedule_balances_all_six_permutations_and_positions() -> None:
    candidates = ("fused", "after-time", "after-predicate")
    orders = complete_permutation_orders(candidates, 30, seed=19)

    assert len(set(orders)) == 6
    assert set(orders.count(order) for order in set(orders)) == {5}
    for candidate in candidates:
        assert [order.index(candidate) for order in orders].count(0) == 10
        assert [order.index(candidate) for order in orders].count(2) == 10


def test_q6_resume_accepts_only_complete_atomic_blocks() -> None:
    config = load_tpch_q6_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_q6_utc_batched_v3.json"
    )
    candidates = ("fused", "after-time", "after-predicate")
    rows = [
        TpchQ6Timing(
            0,
            "block-0",
            " -> ".join(candidates),
            position,
            repeat,
            candidate,
            "2026-07-20T00:00:00+00:00",
            1.0,
            1.0,
            1,
            "sha256:result",
        )
        for position, candidate in enumerate(candidates)
        for repeat in range(config.timed_repeats_per_position)
    ]

    assert _completed_measurement_blocks(rows, config) == {0}
    with pytest.raises(GovernedRealDataSmokeError, match="incomplete persisted block"):
        _completed_measurement_blocks(rows[:-1], config)


def test_q6_v2_requires_odd_batches_without_relaxing_gates() -> None:
    config = load_tpch_q6_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_q6_batched_v2.json"
    )

    assert config.timed_repeats_per_position == 5
    assert config.paired_ratio_outlier_fraction_limit == 0.1
    with pytest.raises(ValueError, match="odd batch"):
        replace(config, timed_repeats_per_position=4)
    with pytest.raises(ValueError, match="scientific label"):
        replace(config, scientific_label="tpch_sf1_q6_formal_development_v1")
    with pytest.raises(ValueError, match="timezone"):
        replace(config, duckdb_timezone="Asia/Shanghai")


def test_q6_v3_binds_exact_semantics_and_utc() -> None:
    config = load_tpch_q6_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_q6_utc_batched_v3.json"
    )

    assert config.timing_protocol == "exact_decimal_utc_batched_v3"
    assert config.duckdb_timezone == "UTC"
    assert config.timed_repeats_per_position == 5
    assert config.paired_ratio_outlier_fraction_limit == 0.1
    assert "decimal" in config.semantic_smoke_path
    with pytest.raises(ValueError, match="inside the project"):
        replace(config, semantic_smoke_path="../outside.json")

    sf10 = replace(
        config,
        scale_factor=10,
        scientific_label=TPCH_Q6_SF10_FORMAL_LABEL,
        results_dir="results/tpch_sf10_q6_formal_v1",
    )
    assert sf10.scale_factor == 10
    with pytest.raises(ValueError, match="not reviewed"):
        replace(sf10, scale_factor=3)

    paired = load_tpch_q6_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_sf10_q6_paired_ci_v2.json"
    )
    assert paired.scientific_label == TPCH_Q6_SF10_PAIRED_CI_LABEL
    assert paired.measured_blocks == 60
    assert set(paired.carryover_candidate_ids) == {
        "materialize-after-q06-time",
        "materialize-after-q06-predicate",
    }
