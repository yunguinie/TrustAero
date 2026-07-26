"""Tests for the V4 profile-versus-paired-label audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trustaero.experiments.optimizer_v4_profile_analysis import (
    analyze_optimizer_v4_profiles,
)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_profile_analysis_keeps_paired_direction_authoritative(tmp_path: Path) -> None:
    run = tmp_path / "profiles" / "run-1"
    _write(run / "summary.json", {"status": "PASS"})
    _write(run / "config.json", {"profile_runs": 1})
    profile = {
        "median_profile_latency_ms": 10.0,
        "profile_latency_samples_ms": [10.0],
        "median_operator_timings_ms": [8.0, 7.0],
    }
    _write(
        run / "families/family.json",
        {
            "family_id": "family",
            "profiles": {
                "early_mask_materialized": profile,
                "late_mask": {**profile, "median_profile_latency_ms": 12.0},
            },
        },
    )
    _write(run / "plans/early.json", {})
    _write(run / "plans/late.json", {})
    audit = tmp_path / "audit.json"
    _write(
        audit,
        {
            "source_run_id": "timing-1",
            "family_audits": [
                {
                    "family_id": "family",
                    "stable_for_transfer_conclusion": True,
                    "median_early_over_late_ratio": 1.1,
                    "paired_median_ratio_ci95": [1.05, 1.2],
                    "directions": {"overall": "late_mask"},
                }
            ],
        },
    )

    result = analyze_optimizer_v4_profiles(run, audit)

    assert result["profile_direction_disagreement_on_stable_count"] == 1
    assert result["profile_direction_is_authoritative_label"] is False
    assert result["paired_timing_direction_is_authoritative_label"] is True
    assert result["operator_timings_are_additive_causal_costs"] is False
    assert result["raw_plan_set_complete"] is True


def test_profile_analysis_rejects_failed_profile_gate(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(run / "summary.json", {"status": "FAIL"})
    with pytest.raises(ValueError, match="passed structural gate"):
        analyze_optimizer_v4_profiles(run, tmp_path / "missing.json")
