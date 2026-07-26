"""Tests for expanded January V4 calibration protocol gates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trustaero.experiments.optimizer_v4_calibration import (
    JanuaryDevelopmentWindow,
    OptimizerV4CalibrationConfig,
    _summarize,
)


def _window(identifier: str, start_day: int, end_day: int) -> JanuaryDevelopmentWindow:
    return JanuaryDevelopmentWindow(
        identifier,
        datetime(2024, 1, start_day, tzinfo=UTC),
        datetime(2024, 1, end_day, tzinfo=UTC),
    )


def _config(**overrides: object) -> OptimizerV4CalibrationConfig:
    values: dict[str, object] = {
        "protocol_name": "test",
        "results_dir": "results/test",
        "windows": (_window("w1", 1, 8), _window("w2", 8, 15)),
        "identifier_widths": (192,),
        "target_match_rates": (0.25,),
        "warmup_blocks": 0,
        "measured_blocks": 2,
        "duckdb_threads": 1,
        "duckdb_memory_limit_mb": 512,
        "order_seed": 1,
        "tie_threshold_fraction": 0.03,
        "require_clean_git": False,
        "profile_analysis_path": "analysis.json",
        "profile_analysis_sha256": "a" * 64,
        "scientific_boundary": "January development only",
    }
    values.update(overrides)
    return OptimizerV4CalibrationConfig(**values)  # type: ignore[arg-type]


def test_calibration_rejects_overlapping_cross_validation_groups() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        _config(windows=(_window("w1", 1, 9), _window("w2", 8, 15)))


def test_calibration_rejects_february_development_data() -> None:
    with pytest.raises(ValueError, match="may not open February"):
        JanuaryDevelopmentWindow(
            "bad",
            datetime(2024, 1, 25, tzinfo=UTC),
            datetime(2024, 2, 2, tzinfo=UTC),
        )


def test_calibration_summary_does_not_claim_a_model() -> None:
    summary = _summarize(
        _config(),
        [
            {
                "status": "PASS",
                "scenario_group": "w1",
                "timings": [{}, {}],
            }
        ],
    )

    assert summary["status"] == "PASS_STRUCTURAL_GATE"
    assert summary["model_fitted"] is False
    assert summary["external_partition_accessed"] is False
    assert summary["grouped_cross_validation_required"] is True
