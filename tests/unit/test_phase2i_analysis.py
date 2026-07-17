"""Tests for deterministic Phase 2I seed-family classification."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import trustaero.experiments.phase2i_analysis as phase2i_analysis
from trustaero.experiments.phase2i_analysis import analyze_phase2i_fragment_run


def _write_source(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "pilot",
                "status": "complete",
                "all_validations_passed": True,
                "unit_count": 4,
                "result_equivalent_fragment_count": 4,
                "distinct_physical_plan_fragment_count": 4,
                "spilled_unit_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (run / "environment.json").write_text(
        json.dumps({"commit_hash": "abc123"}), encoding="utf-8"
    )
    fields = [
        "benchmark",
        "row_count",
        "identifier_width",
        "match_rate",
        "seed",
        "component",
        "median_latency_ms",
    ]
    rows = []
    for match_rate, seed, early, late in (
        (0.1, 1, 200.0, 100.0),
        (0.1, 2, 210.0, 100.0),
        (1.0, 1, 90.0, 100.0),
        (1.0, 2, 98.0, 100.0),
    ):
        common = {
            "benchmark": "mask_fragment",
            "row_count": 1000,
            "identifier_width": 256,
            "match_rate": match_rate,
            "seed": seed,
        }
        rows.extend(
            (
                {**common, "component": "early_mask_fragment", "median_latency_ms": early},
                {**common, "component": "late_mask_fragment", "median_latency_ms": late},
            )
        )
    with (run / "component_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return run


def test_analysis_uses_tie_band_and_complete_seed_families(tmp_path: Path) -> None:
    output = analyze_phase2i_fragment_run(_write_source(tmp_path), tmp_path / "analysis")

    summary = json.loads((output / "analysis_summary.json").read_text(encoding="utf-8"))
    with (output / "family_summary.csv").open(newline="", encoding="utf-8") as handle:
        families = list(csv.DictReader(handle))

    assert summary["unit_classification_counts"] == {"early": 1, "late": 2, "tie": 1}
    assert summary["stable_late_family_count"] == 1
    assert summary["mixed_family_count"] == 1
    assert summary["stable_reversal_observed"] is False
    assert summary["optimizer_design_gate_passes"] is False
    assert {row["family_classification"] for row in families} == {"stable_late", "mixed"}


def test_analysis_rejects_incomplete_validation_evidence(tmp_path: Path) -> None:
    run = _write_source(tmp_path)
    summary_path = run / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["result_equivalent_fragment_count"] = 3
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="equivalence/plan evidence"):
        analyze_phase2i_fragment_run(run, tmp_path / "analysis")


def test_four_of_five_rule_labels_adjacent_early_region() -> None:
    units = []
    for family_id, rows, classes in (
        ("a", 100_000, ("early", "early", "early", "early", "tie")),
        ("b", 150_000, ("early", "early", "early", "early", "late")),
        ("c", 200_000, ("late", "late", "late", "late", "tie")),
    ):
        for seed, classification in enumerate(classes):
            units.append(
                {
                    "family_id": family_id,
                    "row_count": rows,
                    "identifier_width": 256,
                    "match_rate": 1.0,
                    "seed": seed,
                    "early_median_latency_ms": 90.0,
                    "late_median_latency_ms": 100.0,
                    "classification": classification,
                }
            )

    families = phase2i_analysis._family_rows(units, 0.8)

    assert [row["family_classification"] for row in families] == [
        "stable_early",
        "stable_early",
        "stable_late",
    ]
    assert phase2i_analysis._stable_early_region_has_adjacent_families(families) is True


def test_seed_agreement_must_be_a_strict_majority(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be in"):
        analyze_phase2i_fragment_run(
            _write_source(tmp_path),
            tmp_path / "analysis",
            required_seed_agreement_fraction=0.5,
        )


def test_family_relative_difference_preserves_seed_pairing() -> None:
    units = []
    for seed, (early, late) in enumerate(
        ((90.0, 100.0), (180.0, 200.0), (9000.0, 10000.0),
         (90000.0, 100000.0), (5000.0, 4500.0))
    ):
        units.append(
            {
                "family_id": "paired",
                "row_count": 100_000,
                "identifier_width": 128,
                "match_rate": 1.0,
                "seed": seed,
                "early_median_latency_ms": early,
                "late_median_latency_ms": late,
                "classification": "early" if early < late * 0.97 else "late",
            }
        )

    family = phase2i_analysis._family_rows(units, 0.8)[0]

    assert family["family_classification"] == "stable_early"
    assert family["median_early_minus_late_percent"] == pytest.approx(-10.0)
