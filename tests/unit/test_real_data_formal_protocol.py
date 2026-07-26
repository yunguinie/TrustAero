"""Tests for the formal real-data protocol and fail-closed evidence labels."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.bts_mask_join_pilot import load_bts_mask_join_pilot_config
from trustaero.experiments.real_data_candidate_pilot import load_candidate_pilot_config
from trustaero.experiments.real_data_formal_protocol import (
    validate_formal_real_data_protocol,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_repository_formal_protocol_covers_ready_templates() -> None:
    check = validate_formal_real_data_protocol(
        PROJECT_ROOT / "experiments/configs/real_data_formal_protocol_v1.json",
        project_root=PROJECT_ROOT,
    )

    assert check.status == "PASS"
    assert set(check.eligible_template_ids) == {
        "QF-BTS-MASKED-READ",
        "QF-NYC-ZONE-AGGREGATE",
        "QF-BTS-MASK-JOIN-PLACEMENT",
    }
    assert check.deferred_template_ids == ("QF-BTS-NATURAL-MULTIJOIN",)
    assert check.heldout_optimizer_evidence is False


def test_formal_candidate_config_requires_clean_git() -> None:
    config = load_candidate_pilot_config(
        PROJECT_ROOT / "experiments/configs/real_data_formal_candidate_v1.json"
    )

    with pytest.raises(ValueError, match="formal candidate timing controls"):
        replace(config, require_clean_git=False)


def test_formal_mask_config_cannot_claim_optimizer_holdout() -> None:
    config = load_bts_mask_join_pilot_config(
        PROJECT_ROOT / "experiments/configs/bts_mask_join_formal_v1.json"
    )

    with pytest.raises(ValueError, match="not optimizer holdout"):
        replace(config, heldout_optimizer_evidence=True)
