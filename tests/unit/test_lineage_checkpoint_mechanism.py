"""Semantic gates for reusable record-lineage checkpoint candidates."""

from __future__ import annotations

import duckdb
import pytest

from trustaero.experiments.lineage_checkpoint_mechanism import (
    LineageBatchQuery,
    execute_lineage_checkpoint_batch,
)
from trustaero.optimizer.lineage_checkpoint_space import (
    LINEAGE_CHECKPOINT_CANDIDATE_IDS,
    LineageCheckpointStatistics,
    build_lineage_checkpoint_profiles,
)


def _connection() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE lineage_events AS
        SELECT 'event-' || CAST(i AS VARCHAR) AS event_id,
               (hash(i * 17) % 10000)::INTEGER AS policy_bucket,
               (hash(i * 29) % 10000)::INTEGER AS query_bucket,
               (i % 101)::BIGINT AS event_value
        FROM range(5000) AS source(i)
        """
    )
    return connection


def test_three_candidates_return_equal_results_and_record_edges() -> None:
    queries = (
        LineageBatchQuery("q-low", 2000, 3000),
        LineageBatchQuery("q-mid", 5000, 6000),
        LineageBatchQuery("q-high", 8000, 9000),
    )
    connection = _connection()
    try:
        executions = tuple(
            execute_lineage_checkpoint_batch(connection, candidate_id, queries)
            for candidate_id in LINEAGE_CHECKPOINT_CANDIDATE_IDS
        )
    finally:
        connection.close()

    evidence_vectors = {
        tuple(
            (
                item.query_id,
                item.row_count,
                item.result_digest,
                item.edge_digest,
                item.evidence_bytes,
            )
            for item in execution.query_evidence
        )
        for execution in executions
    }
    assert len(evidence_vectors) == 1
    assert executions[0].checkpoint_rows == 0
    assert executions[1].checkpoint_rows > 0
    assert executions[2].checkpoint_rows == 5000


def test_candidate_profiles_expose_reuse_tradeoffs_without_latency_weights() -> None:
    profiles = build_lineage_checkpoint_profiles(
        LineageCheckpointStatistics(
            input_rows=100_000,
            query_count=8,
            distinct_policy_count=2,
            total_result_rows=120_000,
            total_distinct_policy_rows=90_000,
        )
    )

    assert tuple(profile.candidate_id for profile in profiles) == (LINEAGE_CHECKPOINT_CANDIDATE_IDS)
    assert len({profile.result_equivalence_id for profile in profiles}) == 1
    assert all(profile.exposure.raw_rows_materialized == 0 for profile in profiles)


def test_unknown_lineage_checkpoint_candidate_fails_closed() -> None:
    connection = _connection()
    try:
        with pytest.raises(ValueError, match="Unknown lineage checkpoint candidate"):
            execute_lineage_checkpoint_batch(
                connection,
                "invented",
                (LineageBatchQuery("q", 5000, 5000),),
            )
    finally:
        connection.close()
