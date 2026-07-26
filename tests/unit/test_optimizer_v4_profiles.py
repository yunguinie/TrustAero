"""Tests for Optimizer V4 profile controls and structural gates."""

from __future__ import annotations

import pytest

from trustaero.experiments.optimizer_v4_profiles import (
    OptimizerV4ProfileConfig,
    _summarize,
)


def _config(**overrides: object) -> OptimizerV4ProfileConfig:
    values: dict[str, object] = {
        "protocol_name": "test",
        "results_dir": "results/test",
        "identifier_widths": (192,),
        "target_match_rates": (0.25,),
        "profile_runs": 1,
        "duckdb_threads": 1,
        "duckdb_memory_limit_mb": 512,
        "require_clean_git": False,
        "statistics_path": "statistics.json",
        "statistics_sha256": "a" * 64,
        "scientific_boundary": "development only",
    }
    values.update(overrides)
    return OptimizerV4ProfileConfig(**values)  # type: ignore[arg-type]


def test_v4_profile_config_rejects_missing_profiles() -> None:
    with pytest.raises(ValueError, match="profile_runs"):
        _config(profile_runs=0)


def test_v4_profile_summary_keeps_timings_out_of_inference() -> None:
    profile = {
        "shape_stable": True,
        "peak_temp_directory_bytes": 0,
        "timings_are_inference_features": False,
    }
    summary = _summarize(
        [
            {
                "status": "PASS",
                "result_equivalent": True,
                "physical_plans_distinct": True,
                "profiles": {"early": profile, "late": profile},
            }
        ]
    )

    assert summary["status"] == "PASS"
    assert summary["operator_timings_are_inference_features"] is False
    assert summary["model_fitted"] is False


def test_v4_profile_summary_rejects_spill() -> None:
    profile = {
        "shape_stable": True,
        "peak_temp_directory_bytes": 1,
        "timings_are_inference_features": False,
    }
    summary = _summarize(
        [
            {
                "status": "PASS",
                "result_equivalent": True,
                "physical_plans_distinct": True,
                "profiles": {"early": profile, "late": profile},
            }
        ]
    )

    assert summary["status"] == "FAIL"
    assert summary["spilled_profile_count"] == 2
