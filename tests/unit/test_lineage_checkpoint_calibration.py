"""Tests for leakage-safe Lineage checkpoint cost calibration."""

from __future__ import annotations

from pathlib import Path

import pytest

from trustaero.experiments.lineage_checkpoint_calibration import (
    EQUIVALENCE_GROUP,
    evaluate_fixed_baselines,
    load_lineage_calibration_observations,
)
from trustaero.optimizer.lineage_checkpoint_space import (
    LINEAGE_CHECKPOINT_CANDIDATE_IDS,
)


def _formal_run() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "results/lineage_checkpoint_admission_v1/20260726T021127337754Z"
    )


@pytest.mark.local_artifact
def test_loader_builds_complete_candidate_groups_from_frozen_run() -> None:
    observations = load_lineage_calibration_observations(_formal_run())

    assert len(observations) == 6 * 3 * 3
    assert {item.equivalence_group for item in observations} == {EQUIVALENCE_GROUP}
    assert {item.candidate_id for item in observations} == set(LINEAGE_CHECKPOINT_CANDIDATE_IDS)
    assert all(item.features == tuple(sorted(item.features)) for item in observations)


@pytest.mark.local_artifact
def test_each_fixed_baseline_is_evaluated_on_all_decisions() -> None:
    observations = load_lineage_calibration_observations(_formal_run())

    baselines = evaluate_fixed_baselines(
        observations,
        practical_tie_fraction=0.03,
    )

    assert set(baselines) == set(LINEAGE_CHECKPOINT_CANDIDATE_IDS)
    assert all(item["decision_count"] == 18 for item in baselines.values())
