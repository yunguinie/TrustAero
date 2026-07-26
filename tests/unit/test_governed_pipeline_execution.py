"""DuckDB admission tests for governed multi-operator candidates."""

from __future__ import annotations

import json

import duckdb

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.governed_checkpoint_reversal import (
    GovernedCheckpointUnit,
    _create_data,
)
from trustaero.experiments.governed_pipeline_execution import (
    build_executable_governed_pipeline,
    execute_governed_pipeline,
    observe_governed_pipeline_plan,
)
from trustaero.optimizer.governed_pipeline_space import (
    GOVERNED_PIPELINE_CANDIDATE_IDS,
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
)


def _unit() -> GovernedCheckpointUnit:
    return GovernedCheckpointUnit(2_000, 128, 0.3, 0.4, 71)


def test_candidates_are_result_and_record_lineage_equivalent() -> None:
    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, _unit())
        evidence = [
            execute_governed_pipeline(
                connection,
                build_executable_governed_pipeline(candidate_id, _unit()),
            )
            for candidate_id in GOVERNED_PIPELINE_CANDIDATE_IDS
        ]
    finally:
        connection.close()

    assert len({item.result_digest for item in evidence}) == 1
    assert len({item.lineage_digest for item in evidence}) == 1
    assert all(item.row_count > 0 for item in evidence)
    assert all(item.source_event_count == item.row_count for item in evidence)


def test_candidates_have_distinct_combined_physical_plans() -> None:
    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, _unit())
        plans = [
            observe_governed_pipeline_plan(
                connection,
                build_executable_governed_pipeline(candidate_id, _unit()),
            )
            for candidate_id in GOVERNED_PIPELINE_CANDIDATE_IDS
        ]
    finally:
        connection.close()

    assert len({item.combined_fingerprint for item in plans}) == 4
    assert all(item.operator_names for item in plans)


def test_checkpoint_columns_match_declared_exposure() -> None:
    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, _unit())
        for candidate_id in (
            POLICY_FIRST_MASKED_CHECKPOINT,
            QUERY_FIRST_RAW_CHECKPOINT,
            JOIN_FIRST_MASKED_CHECKPOINT,
        ):
            candidate = build_executable_governed_pipeline(candidate_id, _unit())
            for statement in candidate.setup_sql:
                connection.execute(statement)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info('pipeline_checkpoint')").fetchall()
            }
            assert "sensitive_value" not in columns
            assert "masked_value" in columns
            if candidate_id == QUERY_FIRST_RAW_CHECKPOINT:
                raw_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info('raw_query_checkpoint')"
                    ).fetchall()
                }
                assert "sensitive_value" in raw_columns
                assert "masked_value" not in raw_columns
            for table in candidate.temporary_tables:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        connection.close()


def test_query_first_join_reads_only_explicitly_masked_payload() -> None:
    """Prevent DuckDB projection placement from contradicting IR semantics."""

    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, _unit())
        candidate = build_executable_governed_pipeline(
            QUERY_FIRST_RAW_CHECKPOINT,
            _unit(),
        )
        for statement in candidate.setup_sql:
            connection.execute(statement)
        observed = observe_duckdb_plan(
            connection,
            candidate.output_sql,
            analyze=False,
        )
    finally:
        connection.close()

    raw_plan = json.loads(observed.plan_json)
    encoded = json.dumps(raw_plan, sort_keys=True)
    assert "HASH_JOIN" in encoded
    assert "masked_value" in encoded
    assert "sensitive_value" not in encoded
