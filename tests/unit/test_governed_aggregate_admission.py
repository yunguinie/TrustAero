"""Tests for the second governed optimizer-family admission."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from trustaero.experiments.governed_aggregate_admission import (
    GovernedAggregateUnit,
    _create_data,
    _plan_fingerprint,
    _run_candidate,
    aggregate_candidate_sql,
    load_governed_aggregate_admission_config,
)
from trustaero.optimizer.governed_aggregate_space import (
    AGGREGATE_CANDIDATE_IDS,
    GovernedAggregateStatistics,
    plan_governed_aggregate,
)


def test_frozen_config_has_balanced_two_candidate_protocol() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_governed_aggregate_admission_config(
        root / "experiments/configs/governed_aggregate_admission_v1.json"
    )

    assert config.candidate_ids == AGGREGATE_CANDIDATE_IDS
    assert config.blocks_per_unit == 10
    assert len(config.seeds) == 3


def test_candidates_return_equal_aggregates_and_distinct_plans() -> None:
    unit = GovernedAggregateUnit(5_000, 128, 0.5, 0.5, 0.1, 7)
    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, unit)
        results = {
            candidate_id: _run_candidate(connection, candidate_id, unit)[1]
            for candidate_id in AGGREGATE_CANDIDATE_IDS
        }
        lineages = {
            candidate_id: _run_candidate(connection, candidate_id, unit, capture_lineage=True)[2]
            for candidate_id in AGGREGATE_CANDIDATE_IDS
        }
        plans = {
            candidate_id: _plan_fingerprint(connection, candidate_id, unit)
            for candidate_id in AGGREGATE_CANDIDATE_IDS
        }
    finally:
        connection.close()

    assert len(set(results.values())) == 1
    assert len(set(lineages.values())) == 1
    assert len(set(plans.values())) == 2


def test_unknown_candidate_fails_closed() -> None:
    unit = GovernedAggregateUnit(100, 64, 0.5, 0.5, 0.5, 1)

    with pytest.raises(ValueError, match="Unknown governed aggregate candidate"):
        aggregate_candidate_sql("invented", unit)


def test_governance_planner_retains_both_legal_tradeoff_candidates() -> None:
    planning = plan_governed_aggregate(
        GovernedAggregateStatistics(governed_rows=10_000, governed_keys=500)
    )

    assert planning.nondominated_candidate_ids == AGGREGATE_CANDIDATE_IDS
    assert planning.rejected_candidate_ids == ()
    assert planning.dominated_candidate_ids == ()


def test_config_rejects_changed_candidate_space(tmp_path: Path) -> None:
    payload = {
        "results_dir": "results/test",
        "row_count": 100,
        "identifier_width": 64,
        "policy_selectivities": [0.5],
        "query_selectivities": [0.5],
        "key_domain_fractions": [0.5],
        "seeds": [1, 2, 3],
        "candidate_ids": ["invented"],
        "warmup_rounds_per_permutation": 1,
        "measured_rounds_per_permutation": 5,
        "duckdb_threads": 1,
        "duckdb_memory_limit_mb": 128,
        "order_seed": 1,
        "practical_tie_fraction": 0.03,
        "confidence_level": 0.95,
        "bootstrap_draws": 1000,
        "bootstrap_seed": 2,
        "minimum_conclusive_scenario_rate": 0.5,
        "require_clean_git": False,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate set changed"):
        load_governed_aggregate_admission_config(path)
