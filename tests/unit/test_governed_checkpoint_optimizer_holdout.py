"""Safety tests for the frozen governed-checkpoint holdout boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    _load_frozen_model,
    analytic_model_from_dict,
    load_checkpoint_holdout_config,
)
from trustaero.experiments.governed_checkpoint_reversal import (
    load_governed_checkpoint_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_CONFIG = (
    PROJECT_ROOT / "experiments/configs/governed_checkpoint_optimizer_holdout_evaluation_v1.json"
)


def test_holdout_dimensions_are_unseen() -> None:
    config = load_checkpoint_holdout_config(EVALUATION_CONFIG)

    assert not set(config.development_identifier_widths) & set(config.holdout_identifier_widths)
    assert not set(config.development_policy_selectivities) & set(
        config.holdout_policy_selectivities
    )
    assert not set(config.development_query_selectivities) & set(config.holdout_query_selectivities)
    assert not set(config.development_seeds) & set(config.holdout_seeds)


def test_development_value_cannot_be_reintroduced() -> None:
    config = load_checkpoint_holdout_config(EVALUATION_CONFIG)

    with pytest.raises(ValueError, match="reuses development identifier width"):
        replace(config, holdout_identifier_widths=(128, 768))


@pytest.mark.local_artifact
def test_frozen_model_is_bound_to_passed_calibration() -> None:
    config = load_checkpoint_holdout_config(EVALUATION_CONFIG)

    model, hashes = _load_frozen_model(config, PROJECT_ROOT)

    assert model.calibration_id == "ea1-governed-checkpoint-development-v1"
    assert hashes["model"] == config.expected_model_sha256
    assert hashes["development_calibration"] == config.expected_calibration_sha256


def test_direct_winner_classifier_is_rejected() -> None:
    model_path = (
        PROJECT_ROOT / "experiments/frozen/models/governed_checkpoint_optimizer_v2_20260723.json"
    )
    payload = json.loads(model_path.read_text(encoding="utf-8"))
    payload["direct_winner_classifier_used"] = True

    with pytest.raises(ValueError, match="direct winner classifier"):
        analytic_model_from_dict(payload)


def test_measurement_configs_have_distinct_scientific_roles() -> None:
    development = load_governed_checkpoint_config(
        PROJECT_ROOT / "experiments/configs/governed_checkpoint_reversal_v1.json"
    )
    holdout = load_governed_checkpoint_config(
        PROJECT_ROOT / "experiments/configs/governed_checkpoint_optimizer_holdout_v1.json"
    )

    assert development.experiment_role == "development_reversal"
    assert holdout.experiment_role == "frozen_optimizer_holdout"
    assert (
        len(holdout.identifier_widths)
        * len(holdout.policy_selectivities)
        * len(holdout.query_selectivities)
        == 12
    )
