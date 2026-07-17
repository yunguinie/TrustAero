"""Tests for Phase 2L physical operator-role attribution."""

from __future__ import annotations

import pytest

import trustaero.experiments.pipeline_attribution as attribution


def _row(
    name: str,
    timing: float,
    *,
    rows_scanned: int = 0,
) -> dict[str, str]:
    return {
        "operator_name": name,
        "median_operator_timing_ms": str(timing),
        "rows_scanned": str(rows_scanned),
    }


def test_role_mapping_accounts_for_complete_early_shape() -> None:
    rows = [
        _row("EXPLAIN_ANALYZE", 0.001),
        _row("BATCH_CREATE_TABLE_AS", 5.0),
        _row("CTE", 4.0),
        _row("PROJECTION", 100.0),
        _row("SEQ_SCAN", 3.0, rows_scanned=100_000),
        _row("PROJECTION", 2.0),
        _row("ORDER_BY", 20.0),
        _row("HASH_JOIN", 10.0),
        _row("CTE_SCAN", 1.0),
        _row("SEQ_SCAN", 0.5, rows_scanned=10_000),
    ]

    roles = attribution._operator_role_totals(rows)

    assert roles["hash_projection"] == 100.0
    assert roles["support_projection"] == 2.0
    assert roles["materialization"] == 5.0
    assert roles["event_scan"] == 3.0
    assert sum(roles.values()) == pytest.approx(145.5)


def test_unknown_operator_shape_fails_closed() -> None:
    rows = [
        _row("BATCH_CREATE_TABLE_AS", 1.0),
        _row("PROJECTION", 2.0),
        _row("HASH_JOIN", 1.0),
        _row("ORDER_BY", 1.0),
        _row("SEQ_SCAN", 1.0, rows_scanned=100),
        _row("SEQ_SCAN", 1.0, rows_scanned=10),
        _row("MAGIC_UNKNOWN", 5.0),
    ]

    with pytest.raises(ValueError, match="Unknown physical operators"):
        attribution._operator_role_totals(rows)


def test_spearman_handles_monotone_values_and_ties() -> None:
    assert attribution._spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == pytest.approx(1.0)
    assert attribution._spearman([1.0, 1.0, 2.0], [3.0, 3.0, 1.0]) == pytest.approx(-1.0)


def test_classification_matches_frozen_multiplicative_tie_rule() -> None:
    assert attribution._classify(__import__("math").log(0.96), 0.03) == "early"
    assert attribution._classify(__import__("math").log(1.02), 0.03) == "tie"
    assert attribution._classify(__import__("math").log(1.04), 0.03) == "late"
