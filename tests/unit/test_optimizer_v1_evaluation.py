"""Tests for measured evaluation of Mask Optimizer V1."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from trustaero.experiments.optimizer_v1 import evaluate_mask_optimizer_v1


def _write_strategy_rows(path: Path) -> None:
    rows: list[dict[str, object]] = []
    for seed in (1, 2):
        unit_id = f"wide_high_match-n300000-seed{seed}"
        common: dict[str, object] = {
            "unit_id": unit_id,
            "scenario_id": "wide_high_match",
            "row_count": 300_000,
            "data_seed": seed,
            "after_policy_rows": 300_000,
            "after_join_rows": 300_000,
            "mask_rows_processed": 300_000,
        }
        rows.extend(
            [
                {
                    **common,
                    "strategy_id": "fused",
                    "median_governed_latency_ms": 120.0,
                    "raw_sensitive_rows_exposed_to_join": 300_000,
                },
                {
                    **common,
                    "strategy_id": "early-physical-id-may-change",
                    "median_governed_latency_ms": 100.0,
                    "raw_sensitive_rows_exposed_to_join": 0,
                },
            ]
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_evaluator_uses_semantics_not_brittle_early_strategy_id(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "optimizer-test",
                "status": "complete",
                "all_results_equivalent": True,
                "tie_threshold_fraction": 0.03,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "scenarios": [
                    {"scenario_id": "wide_high_match", "identifier_width": 1024}
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_strategy_rows(tmp_path / "strategy_summary.csv")

    output = evaluate_mask_optimizer_v1(tmp_path, evaluation_label="held_out")

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    with (output / "cases.csv").open(newline="", encoding="utf-8") as handle:
        cases = list(csv.DictReader(handle))
    assert summary["case_count"] == 2
    assert summary["exact_top1_rate"] == 1.0
    assert summary["median_regret_percent"] == 0.0
    assert summary["geometric_speedup_vs_fixed_late_ratio"] == pytest.approx(1.2)
    assert (output / "scenario_summary.csv").exists()
    assert {row["selected_strategy_id"] for row in cases} == {
        "early-physical-id-may-change"
    }


def test_evaluator_marks_confirmation_as_calibration(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "all_results_equivalent": True,
                "tie_threshold_fraction": 0.03,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "scenarios": [
                    {"scenario_id": "wide_high_match", "identifier_width": 1024}
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_strategy_rows(tmp_path / "strategy_summary.csv")

    output = evaluate_mask_optimizer_v1(tmp_path, evaluation_label="calibration")
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

    assert "do not measure generalization" in summary["interpretation_limit"]
