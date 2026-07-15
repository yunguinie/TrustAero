"""Tests for controlled Phase 2 synthetic DuckDB statistics."""

from __future__ import annotations

import pytest

from trustaero.experiments.synthetic import (
    SyntheticDataConfig,
    generate_synthetic_workload,
)


def test_generator_realizes_six_controlled_statistics() -> None:
    """Observed DuckDB counts should match the requested deterministic fractions."""

    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    try:
        stats = generate_synthetic_workload(
            connection,
            SyntheticDataConfig(
                workload_id="controlled",
                row_count=1000,
                temporal_selectivity=0.10,
                spatial_selectivity=0.20,
                policy_selectivity=0.30,
                join_match_rate=0.80,
                hot_key_fraction=0.10,
                seed=7,
            ),
        )
    finally:
        connection.close()

    assert stats.row_count == 1000
    assert stats.temporal_rows == 100
    assert stats.spatial_rows == 200
    assert stats.policy_rows == 300
    assert stats.join_matched_rows == 800
    assert stats.hot_key_rows == 100
    assert stats.dimension_row_count == 701


@pytest.mark.parametrize(
    "updates",
    [
        {"row_count": 0},
        {"spatial_selectivity": 1.1},
        {"hot_key_fraction": 0.6, "join_match_rate": 0.5},
    ],
)
def test_generator_config_rejects_invalid_controls(updates: dict[str, object]) -> None:
    """Invalid workload controls fail before any SQL is executed."""

    values: dict[str, object] = {
        "workload_id": "invalid",
        "row_count": 10,
        "temporal_selectivity": 0.5,
        "spatial_selectivity": 0.5,
        "policy_selectivity": 0.5,
        "join_match_rate": 0.5,
        "hot_key_fraction": 0.1,
    }
    values.update(updates)

    with pytest.raises(ValueError):
        SyntheticDataConfig(**values)  # type: ignore[arg-type]
