"""Tests for actual DuckDB physical-plan observation and fingerprinting."""

from __future__ import annotations

import pytest

from trustaero.execution import observe_duckdb_plan


def test_repeated_actual_plan_has_stable_fingerprint_and_metrics() -> None:
    """Runtime timings may vary, but one physical structure has one fingerprint."""

    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE events AS SELECT i AS event_id, i % 2 = 0 AS allowed "
            "FROM range(100) generated(i)"
        )
        first = observe_duckdb_plan(
            connection,
            "SELECT * FROM events WHERE allowed = ?",
            (True,),
        )
        second = observe_duckdb_plan(
            connection,
            "SELECT * FROM events WHERE allowed = ?",
            (True,),
        )
    finally:
        connection.close()

    assert first.fingerprint == second.fingerprint
    assert "SEQ_SCAN" in first.operator_names
    assert max(first.actual_cardinalities) == 50
    assert any(value >= 100 for value in first.rows_scanned)
    assert first.profile_latency_ms > 0
    assert first.peak_buffer_memory_bytes >= 0
    assert first.peak_temp_directory_bytes >= 0
    assert first.total_memory_allocated_bytes >= 0


def test_materialization_changes_observed_physical_plan() -> None:
    """A real CTE materialization must differ from a flattened filter tree."""

    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE events AS SELECT i AS event_id, i % 2 = 0 AS allowed "
            "FROM range(100) generated(i)"
        )
        fused = observe_duckdb_plan(
            connection,
            "SELECT * FROM events WHERE allowed",
        )
        materialized = observe_duckdb_plan(
            connection,
            "WITH filtered AS MATERIALIZED (SELECT * FROM events WHERE allowed) "
            "SELECT * FROM filtered",
        )
    finally:
        connection.close()

    assert fused.fingerprint != materialized.fingerprint
    assert "CTE" in materialized.operator_names
    assert "CTE_SCAN" in materialized.operator_names
