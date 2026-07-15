"""Tests for Phase 2C balancing, checkpoints, and result artifacts."""

from __future__ import annotations

import csv
import json
from dataclasses import replace

import pytest

import trustaero.experiments.phase2c as phase2c
from trustaero.experiments.phase2c import (
    Phase2CConfig,
    Phase2CScenario,
    balanced_candidate_orders,
    run_phase2c,
)


def _unit_config() -> Phase2CConfig:
    return Phase2CConfig(
        results_dir="results/test_phase2c_unit",
        scenarios=(
            Phase2CScenario(
                scenario_id="unit",
                temporal_selectivity=0.6,
                spatial_selectivity=0.5,
                policy_selectivity=0.4,
                join_match_rate=0.8,
                hot_key_fraction=0.1,
            ),
        ),
        row_counts=(1000,),
        seeds=(2,),
        warmup_runs=0,
        measured_runs=1,
    )


def test_balanced_orders_rotate_every_candidate_through_every_position() -> None:
    strategy_ids = ("a", "b", "c", "d")
    orders = balanced_candidate_orders(strategy_ids, 8, offset_seed=3)

    assert len(orders) == 8
    for position in range(4):
        counts = {strategy_id: 0 for strategy_id in strategy_ids}
        for order in orders:
            counts[order[position]] += 1
        assert set(counts.values()) == {2}


def test_phase2c_writes_complete_checkpoint_and_resumes_without_duplication() -> None:
    pytest.importorskip("duckdb")
    config = _unit_config()

    output_dir = run_phase2c(config)
    checkpoint = json.loads((output_dir / "checkpoint.json").read_text(encoding="utf-8"))
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    with (output_dir / "raw_measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = tuple(csv.DictReader(handle))

    assert checkpoint["status"] == "complete"
    assert len(checkpoint["completed_units"]) == 1
    assert summary["all_results_equivalent"] is True
    assert summary["measurement_count"] == 4
    assert len(rows) == 4
    assert (output_dir / "progress.json").exists()
    assert (output_dir / "confidence_intervals.json").exists()

    resumed = run_phase2c(config, resume_run_id=output_dir.name)
    with (resumed / "raw_measurements.csv").open(newline="", encoding="utf-8") as handle:
        resumed_rows = tuple(csv.DictReader(handle))
    assert resumed == output_dir
    assert len(resumed_rows) == len(rows)

    original_commit = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))[
        "commit_hash"
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(phase2c, "_git_commit", lambda _root: original_commit + "-changed")
        with pytest.raises(ValueError, match="Git commit changed"):
            run_phase2c(config, resume_run_id=output_dir.name)


def test_paper_protocol_rejects_dirty_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phase2c, "_git_dirty", lambda _root: True)

    with pytest.raises(ValueError, match="clean Git worktree"):
        run_phase2c(replace(_unit_config(), require_clean_git=True))
