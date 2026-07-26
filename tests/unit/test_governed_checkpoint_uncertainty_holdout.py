"""Scientific-boundary tests for the V3.1 independent holdout."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.governed_checkpoint_uncertainty_holdout import (
    _load_guard,
    load_uncertainty_holdout_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = (
    PROJECT_ROOT / "experiments/configs/governed_checkpoint_uncertainty_holdout_evaluation_v1.json"
)


def test_holdout_excludes_all_consumed_dimensions() -> None:
    config = load_uncertainty_holdout_config(CONFIG_PATH)

    assert not set(config.excluded_identifier_widths) & set(config.holdout_identifier_widths)
    assert not set(config.excluded_policy_selectivities) & set(config.holdout_policy_selectivities)
    assert not set(config.excluded_query_selectivities) & set(config.holdout_query_selectivities)
    assert not set(config.excluded_seeds) & set(config.holdout_seeds)


def test_consumed_value_cannot_be_reintroduced() -> None:
    config = load_uncertainty_holdout_config(CONFIG_PATH)

    with pytest.raises(ValueError, match="reuses consumed identifier width"):
        replace(config, holdout_identifier_widths=(256, 640))


@pytest.mark.local_artifact
def test_guard_is_hash_bound_to_passed_calibration() -> None:
    config = load_uncertainty_holdout_config(CONFIG_PATH)

    guard, hashes = _load_guard(config, PROJECT_ROOT)

    assert guard.query_margin_error_upper_ms == pytest.approx(2.37896036460923)
    assert guard.calibration_family_count == 4
    assert hashes["guard"] == config.expected_guard_sha256
    assert hashes["development_calibration"] == config.expected_calibration_sha256
