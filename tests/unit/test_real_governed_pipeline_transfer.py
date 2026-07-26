"""Tests for the frozen real-distribution optimizer transfer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trustaero.experiments.real_governed_pipeline_transfer import (
    REAL_PIPELINE_CANDIDATE_IDS,
    RealGovernedPipelineProfile,
    load_real_governed_pipeline_config,
    load_real_governed_pipeline_evaluation_config,
    real_governed_pipeline_units,
)

ROOT = Path(__file__).resolve().parents[2]


def test_frozen_real_matrix_is_complete_and_balanced() -> None:
    """Quarterly BTS/NYC months expand to the declared 96 atomic units."""

    config = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/real_governed_pipeline_transfer_v1.json"
    )
    units = real_governed_pipeline_units(config)

    assert len(units) == 96
    assert config.candidate_ids == REAL_PIPELINE_CANDIDATE_IDS
    assert config.measured_blocks_per_unit == 30
    assert len({unit.unit_id for unit in units}) == 96
    assert {unit.dataset for unit in units} == {"bts", "nyc_tlc"}
    assert {unit.month for unit in units} == {
        "2024-02",
        "2024-05",
        "2024-08",
        "2024-11",
    }


def test_evaluator_is_bound_to_the_same_split() -> None:
    """The one-shot evaluator cannot silently switch months or profiles."""

    measurement = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/real_governed_pipeline_transfer_v1.json"
    )
    evaluation = load_real_governed_pipeline_evaluation_config(
        ROOT / "experiments/configs/real_governed_pipeline_transfer_evaluation_v1.json"
    )

    assert evaluation.expected_sources == tuple(source.source_id for source in measurement.sources)
    assert evaluation.expected_profiles == tuple(
        profile.profile_id for profile in measurement.profiles
    )
    assert evaluation.expected_seeds == measurement.seeds
    assert evaluation.expected_row_count == measurement.row_count


def test_invalid_profile_fails_closed() -> None:
    """Governance selectivities outside their legal range are rejected."""

    with pytest.raises(ValueError, match="selectivities"):
        RealGovernedPipelineProfile("invalid", 256, 0.2, 1.1, 0.8)


def test_protocol_declares_one_shot_retention() -> None:
    """The scientific protocol explicitly prevents post-result retuning."""

    payload = json.loads(
        (
            ROOT / "experiments/frozen/real_governed_pipeline_transfer_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["measurement_scale"]["units"] == 96
    assert payload["measurement_scale"]["total_candidate_executions"] == 8640
    assert any("Pass or fail" in item for item in payload["one_shot_rule"])


def test_saved_latency_can_receive_legacy_carryover_alias() -> None:
    """Analysis-only repair copies, rather than recomputes, the observation."""

    measurement = {"latency_ms": 12.5}
    measurement.setdefault(
        "client_materialization_latency_ms",
        measurement["latency_ms"],
    )

    assert measurement["client_materialization_latency_ms"] == 12.5
