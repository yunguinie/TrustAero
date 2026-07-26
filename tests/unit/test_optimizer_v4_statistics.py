"""Tests for label-free real-pipeline V4 statistic extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import trustaero.experiments.optimizer_v4_statistics as statistics_module
from trustaero.experiments.optimizer_v4_statistics import (
    extract_january_pipeline_statistics,
)
from trustaero.experiments.real_data_governed import GovernedRealDataSmokeError


class _Cursor:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...]:
        return self._row


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = iter(rows)

    def execute(self, _query: str) -> _Cursor:
        return _Cursor(next(self._rows))


def _patch_controlled_views(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        statistics_module,
        "_controlled_views",
        lambda *_args, **_kwargs: (None, 100, 0.7, 2),
    )


def test_extract_v4_statistics_uses_pre_execution_catalog_quantities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_controlled_views(monkeypatch)
    connection = _Connection(
        [
            (1_000, 31.0),
            (2, 28.0, 20.0, 3.0),
            (70,),
        ]
    )

    stats, metadata = extract_january_pipeline_statistics(
        connection, Path("unused"), width=384, target_match_rate=0.7
    )

    assert stats.source_scan_rows == 1_000
    assert stats.join_input_rows == 100
    assert stats.join_output_rows_estimate == 70
    assert stats.dimension_build_payload_width_bytes == 28.0
    assert stats.dimension_output_payload_width_bytes == 20.0
    assert stats.sort_key_width_bytes == 11.0
    assert metadata["candidate_runtime_observed"] is False
    assert metadata["oracle_label_observed"] is False
    assert metadata["external_partition_accessed"] is False


def test_extract_v4_statistics_rejects_inexact_controlled_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_controlled_views(monkeypatch)
    connection = _Connection(
        [
            (1_000, 31.0),
            (2, 28.0, 20.0, 3.0),
            (69,),
        ]
    )

    with pytest.raises(GovernedRealDataSmokeError, match="not exact"):
        extract_january_pipeline_statistics(
            connection, Path("unused"), width=384, target_match_rate=0.7
        )
