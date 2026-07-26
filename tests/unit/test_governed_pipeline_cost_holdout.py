"""Tests for frozen governed pipeline holdout selection."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
)
from trustaero.experiments.governed_pipeline_cost_calibration import (
    EQUIVALENCE_GROUP,
)
from trustaero.experiments.governed_pipeline_cost_holdout import (
    _select_with_frozen_model,
    load_governed_pipeline_cost_holdout_config,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/governed_pipeline_cost_holdout_evaluation_v1.json"


def test_holdout_factors_are_new_and_model_is_digest_bound() -> None:
    config = load_governed_pipeline_cost_holdout_config(CONFIG)

    assert config.expected_factors["row_count"] == 120_000
    assert config.expected_factors["identifier_widths"] == [256, 768]
    assert len(config.model_sha256) == 64
    assert config.minimum_oracle_set_hit_rate == 0.8


def test_frozen_selector_ranks_cost_without_refitting() -> None:
    observations = (
        CalibrationObservation(
            "s",
            1,
            EQUIVALENCE_GROUP,
            "a",
            1.0,
            (("work", 1.0),),
        ),
        CalibrationObservation(
            "s",
            1,
            EQUIVALENCE_GROUP,
            "b",
            2.0,
            (("work", 3.0),),
        ),
    )
    model = {
        "coefficients": {"work": 2.0},
        "intercept_ms": 1.0,
        "practical_tie_fraction": 0.03,
        "stable_preference_candidate_id": "b",
    }

    selected, predictions = _select_with_frozen_model(observations, model)

    assert next(iter(selected.values())) == "a"
    assert predictions[0]["selected_candidate_id"] == "a"
