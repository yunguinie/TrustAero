"""Pure contract tests for reproducible TPC-H preparation."""

from __future__ import annotations

from pathlib import Path

import pytest

from trustaero.data.tpch import (
    TPCH_SF1_EXPECTED_ROWS,
    TPCH_SF10_EXPECTED_ROWS,
    TpchPreparationError,
    _inside,
    _resume_partition,
    _validate_database,
)


def test_tpch_sf1_expected_rows_match_standard_scale() -> None:
    assert TPCH_SF1_EXPECTED_ROWS["orders"] == 1_500_000
    assert TPCH_SF1_EXPECTED_ROWS["lineitem"] == 6_001_215
    assert sum(TPCH_SF1_EXPECTED_ROWS.values()) == 8_661_245


def test_tpch_sf10_expected_rows_are_explicit_not_sf1_extrapolation() -> None:
    assert TPCH_SF10_EXPECTED_ROWS["orders"] == 15_000_000
    assert TPCH_SF10_EXPECTED_ROWS["lineitem"] == 59_986_052
    assert sum(TPCH_SF10_EXPECTED_ROWS.values()) == 86_586_082


def test_unreviewed_scale_is_rejected_before_database_queries() -> None:
    with pytest.raises(TpchPreparationError, match="no reviewed exact-row contract"):
        _validate_database(object(), scale_factor=3)


def test_tpch_build_checkpoint_resumes_only_matching_committed_source(
    tmp_path: Path,
) -> None:
    building = tmp_path / "tpch_sf10.duckdb.building"
    checkpoint = tmp_path / "tpch_sf10.duckdb.building.state.json"
    building.write_bytes(b"partial database")
    checkpoint.write_text(
        '{"scale_factor":10,"source_commit":"abc","completed_partitions":3}',
        encoding="utf-8",
    )

    assert (
        _resume_partition(
            building,
            checkpoint,
            scale_factor=10,
            source_commit="abc",
        )
        == 3
    )
    assert (
        _resume_partition(
            building,
            checkpoint,
            scale_factor=10,
            source_commit="different",
        )
        == 0
    )
    assert not building.exists()
    assert not checkpoint.exists()


def test_tpch_paths_cannot_escape_project_root(tmp_path: Path) -> None:
    assert _inside(tmp_path, tmp_path / "data/processed/tpch.duckdb").is_relative_to(tmp_path)
    with pytest.raises(TpchPreparationError, match="escapes"):
        _inside(tmp_path, tmp_path.parent / "outside.duckdb")
