"""Tests for paired, seed-level Phase 2C analysis."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from trustaero.experiments.phase2c_analysis import analyze_phase2c_run


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _completed_fixture(tmp_path: Path) -> Path:
    summary = {
        "run_id": "paired-test",
        "status": "complete",
        "unit_count": 5,
        "all_results_equivalent": True,
        "tie_threshold_fraction": 0.03,
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    rows: list[dict[str, object]] = []
    for seed, fused_ms, ordered_ms in (
        (1, 10.0, 8.0),
        (2, 12.0, 9.0),
        (3, 11.0, 8.5),
        (4, 10.5, 8.2),
        (5, 11.5, 8.8),
    ):
        for strategy_id, latency in (
            ("fused", fused_ms),
            ("ordered-policy-first", ordered_ms),
        ):
            rows.append(
                {
                    "scenario_id": "policy_selective",
                    "row_count": 100000,
                    "data_seed": seed,
                    "strategy_id": strategy_id,
                    "median_governed_latency_ms": latency,
                }
            )
    _write_csv(tmp_path / "strategy_summary.csv", rows)
    return tmp_path


def test_analysis_finds_stable_paired_reversal_and_preserves_raw_files(
    tmp_path: Path,
) -> None:
    run_dir = _completed_fixture(tmp_path)
    source_before = (run_dir / "strategy_summary.csv").read_bytes()

    output_dir = analyze_phase2c_run(run_dir, bootstrap_runs=200)

    summary = json.loads((output_dir / "analysis_summary.json").read_text(encoding="utf-8"))
    with (output_dir / "scenario_summary.csv").open(newline="", encoding="utf-8") as handle:
        scenarios = list(csv.DictReader(handle))
    assert summary["stable_nonfused_reversal_count"] == 1
    assert scenarios[0]["best_strategy_by_seed_median"] == "ordered-policy-first"
    assert scenarios[0]["stable_nonfused_reversal"] == "True"
    assert (output_dir / "report.md").exists()
    assert (run_dir / "strategy_summary.csv").read_bytes() == source_before


def test_analysis_rejects_incomplete_run(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "status": "running",
                "all_results_equivalent": True,
                "tie_threshold_fraction": 0.03,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="complete"):
        analyze_phase2c_run(tmp_path)
