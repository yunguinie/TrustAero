"""Tests for the Phase 1 DuckDB execution experiment runner."""

from __future__ import annotations

import json

import pytest

from trustaero.experiments.models import Phase1Config
from trustaero.experiments.phase1 import run_phase1


def test_run_phase1_writes_repeatable_execution_artifacts() -> None:
    """Phase 1 should produce paper-style artifacts for the DuckDB smoke path."""

    pytest.importorskip("duckdb")

    output_dir = run_phase1(
        Phase1Config(
            results_dir="results/test_phase1_unit",
            warmup_runs=0,
            measured_runs=1,
        )
    )

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["all_correct"] is True
    assert summary["row_count"] == 2
    assert summary["unverified_components"] == ["physical_plan_execution"]
    assert (output_dir / "cases.csv").exists()
    assert (output_dir / "environment.json").exists()
    assert (output_dir / "config.json").exists()
