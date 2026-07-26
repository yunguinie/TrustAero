"""Tests for governed pipeline physical-work cost calibration."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
)
from trustaero.experiments.governed_pipeline_cost_calibration import (
    EQUIVALENCE_GROUP,
    fixed_candidate_baselines,
    load_governed_pipeline_cost_calibration_config,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/governed_pipeline_cost_calibration_v1.json"


def _observation(
    scenario: str,
    candidate: str,
    latency: float,
) -> CalibrationObservation:
    return CalibrationObservation(
        scenario_id=scenario,
        seed=1,
        equivalence_group=EQUIVALENCE_GROUP,
        candidate_id=candidate,
        latency_ms=latency,
        features=(("work.rows", latency),),
    )


def test_frozen_calibration_config_has_grouped_stop_go_gates() -> None:
    config = load_governed_pipeline_cost_calibration_config(CONFIG)

    assert config.lambda_grid == (10.0, 100.0, 1000.0)
    assert config.minimum_oracle_set_hit_rate == 0.8
    assert config.minimum_selected_candidate_count == 2
    assert config.require_not_worse_than_best_fixed_mean is True


def test_every_fixed_candidate_is_reported_as_a_baseline() -> None:
    observations = (
        _observation("s1", "a", 1.0),
        _observation("s1", "b", 2.0),
        _observation("s1", "c", 3.0),
        _observation("s2", "a", 2.0),
        _observation("s2", "b", 1.0),
        _observation("s2", "c", 2.0),
    )

    baselines = fixed_candidate_baselines(
        observations,
        practical_tie_fraction=0.03,
    )

    assert set(baselines) == {"a", "b", "c"}
    assert baselines["c"]["mean_regret_percent"] > baselines["a"]["mean_regret_percent"]
