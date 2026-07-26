"""Tests for the final lineage-checkpoint timing admission."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trustaero.experiments.lineage_checkpoint_admission import (
    _create_data,
    load_lineage_checkpoint_admission_config,
)
from trustaero.experiments.lineage_checkpoint_mechanism import (
    execute_lineage_checkpoint_batch,
    observe_lineage_checkpoint_plan,
)
from trustaero.optimizer.lineage_checkpoint_space import (
    LINEAGE_CHECKPOINT_CANDIDATE_IDS,
)


def test_frozen_admission_has_balanced_complete_scenarios() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_lineage_checkpoint_admission_config(
        root / "experiments/configs/lineage_checkpoint_admission_v1.json"
    )

    assert config.candidate_ids == LINEAGE_CHECKPOINT_CANDIDATE_IDS
    assert len(config.scenarios) == 6
    assert config.blocks_per_unit == 30
    assert len(config.seeds) == 3


def test_observed_candidate_plans_are_distinct_and_evidence_equal() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_lineage_checkpoint_admission_config(
        root / "experiments/configs/lineage_checkpoint_admission_smoke_v1.json"
    )
    queries = config.scenarios[1].queries()
    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, 2_000, 3)
        plans = {
            candidate_id: observe_lineage_checkpoint_plan(connection, candidate_id, queries)
            for candidate_id in config.candidate_ids
        }
        executions = tuple(
            execute_lineage_checkpoint_batch(connection, candidate_id, queries)
            for candidate_id in config.candidate_ids
        )
    finally:
        connection.close()

    evidence = {
        tuple((item.result_digest, item.edge_digest) for item in execution.query_evidence)
        for execution in executions
    }
    assert len(set(plans.values())) == 3
    assert len(evidence) == 1
