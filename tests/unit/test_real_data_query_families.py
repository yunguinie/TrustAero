"""Tests for the pre-performance real-data query-family freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trustaero.experiments.real_data_query_families import (
    load_query_family_protocol,
    validate_query_family_protocol,
)


def test_frozen_query_family_protocol_validates_semantic_ready_plans() -> None:
    root = Path(__file__).resolve().parents[2]
    protocol = root / "experiments/configs/real_data_query_families_v1.json"

    check = validate_query_family_protocol(protocol, project_root=root)

    assert check.status == "PASS"
    assert check.semantic_ready_count == 4
    assert check.design_only_count == 2
    assert check.performance_ready is False
    ready = [item for item in check.templates if item.stage == "semantic_ready"]
    assert all(item.status == "PASS" for item in ready)
    assert all(item.validation_status == "REWRITE" for item in ready)
    assert [len(item.candidate_ids) for item in ready] == [3, 3, 4, 2]
    pending = [item for item in check.templates if item.stage == "design_only"]
    assert all(item.status == "PENDING" and item.diagnostics for item in pending)


def test_v1_query_family_protocol_remains_byte_immutable() -> None:
    """Past measurements bind this exact digest; extensions must use a new version."""

    root = Path(__file__).resolve().parents[2]
    protocol = root / "experiments/configs/real_data_query_families_v1.json"

    assert hashlib.sha256(protocol.read_bytes()).hexdigest() == (
        "1f1ac460f346b1f660aef0b98fe9a6d92c45fb9bad14765c542e39ac947e9425"
    )


def test_design_only_template_cannot_claim_performance_eligibility(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "experiments/configs/real_data_query_families_v1.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["templates"][4]["performance_eligible"] = True
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="design-only templates cannot be performance"):
        load_query_family_protocol(invalid)


def test_semantic_template_candidate_expectations_cannot_drift(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "experiments/configs/real_data_query_families_v1.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["templates"][0]["governance_profiles"][0]["expected_feasible_candidates"].remove(
        "fused"
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="profile candidates do not match"):
        validate_query_family_protocol(invalid, project_root=root)
