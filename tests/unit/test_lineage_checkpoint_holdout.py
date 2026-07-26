"""Pre-run checks for the frozen independent Lineage holdout."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.lineage_checkpoint_admission import (
    load_lineage_checkpoint_admission_config,
)


def test_holdout_dimensions_are_disjoint_from_development() -> None:
    root = Path(__file__).resolve().parents[2]
    development = load_lineage_checkpoint_admission_config(
        root / "experiments/configs/lineage_checkpoint_admission_v1.json"
    )
    holdout = load_lineage_checkpoint_admission_config(
        root / "experiments/configs/lineage_checkpoint_holdout_v1.json"
    )

    assert holdout.row_count != development.row_count
    assert set(holdout.seeds).isdisjoint(development.seeds)
    assert {scenario.query_count for scenario in holdout.scenarios}.isdisjoint(
        scenario.query_count for scenario in development.scenarios
    )


def test_frozen_model_is_bound_to_passed_calibration() -> None:
    root = Path(__file__).resolve().parents[2]
    model = json.loads(
        (
            root / "experiments/frozen/models/lineage_checkpoint_cost_model_v1_20260726.json"
        ).read_text(encoding="utf-8")
    )

    assert model["status"] == "FROZEN_AFTER_GROUPED_DEVELOPMENT"
    assert model["development_result"]["grouped_oracle_set_hit_rate"] == 1.0
