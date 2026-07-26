"""Contracts for the one-shot SF10 multi-candidate confirmation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trustaero.experiments.tpch_multicandidate_scale_confirmation import (
    _bind_scale,
    load_tpch_scale_confirmation_config,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/tpch_multicandidate_scale_confirmation_v2.json"


def test_scale_binding_changes_only_content_addressing_labels() -> None:
    raw = {
        "dataset": "tpch_sf1_orders",
        "snapshot": "sf1-64a709fa8f99",
        "plan_id": "tpch-sf1-q03",
        "predicate": {"value": "BUILDING"},
    }
    bound = _bind_scale(raw, "sf10-fb3788ec8cb5")
    assert bound["dataset"] == "tpch_sf10_orders"
    assert bound["snapshot"] == "sf10-fb3788ec8cb5"
    assert bound["plan_id"] == "tpch-sf10-q03"
    assert bound["predicate"] == raw["predicate"]


def test_scale_confirmation_is_balanced_and_one_shot() -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    # The digest placeholder is replaced with the committed SF1 negative
    # before this test is run in the final patch.
    assert payload["prior_negative_sha256"] != "PLACEHOLDER_PRIOR_NEGATIVE_SHA256"
    config = load_tpch_scale_confirmation_config(CONFIG)
    assert config.scale_factor == 10
    assert config.measured_rounds_per_permutation == 5
    assert 6 * config.measured_rounds_per_permutation == 30
    assert all(len(targets) == 2 for _query, targets in config.query_targets)


def test_confirmation_rejects_a_weaker_round_count(tmp_path: Path) -> None:
    payload = CONFIG.read_text(encoding="utf-8").replace(
        '"measured_rounds_per_permutation": 5',
        '"measured_rounds_per_permutation": 4',
    )
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="five rounds"):
        load_tpch_scale_confirmation_config(path)
