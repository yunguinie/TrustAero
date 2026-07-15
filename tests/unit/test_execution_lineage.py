"""Tests for the deliberately small source-lineage instrumentation fragment."""

from __future__ import annotations

import copy

import pytest

from trustaero.execution import LineageInstrumentationError, capture_source_lineage
from trustaero.ir.enums import LineageLevel, ObligationType, ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.validator.service import validate


def _lineage_plan(accept_plan, policy_set, catalog, level: str):
    policy_raw = policy_set.model_dump(mode="json")
    policy_raw["rules"][0]["obligations"] = [
        {"obligation_type": "LINEAGE_CAPTURE", "parameters": {"level": level}}
    ]
    response = validate(copy.deepcopy(accept_plan), PolicySet.model_validate(policy_raw), catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    return response.validated_plan


def test_source_lineage_binds_result_to_resolved_dataset_snapshot(
    accept_plan, policy_set, catalog
) -> None:
    """Source instrumentation emits checkable evidence and a stable digest."""

    plan = _lineage_plan(accept_plan, policy_set, catalog, "source")

    captured = capture_source_lineage(
        plan,
        execution_id="execution-1",
        result_id="sha256:result-1",
    )

    assert captured.evidence is not None
    assert captured.evidence.lineage_level == LineageLevel.SOURCE
    assert captured.evidence.covered_operators == ("op2",)
    assert captured.evidence.edge_digest.startswith("sha256:")
    assert captured.lineage_digest is not None
    assert captured.source_count == 1
    assert captured.latency_ms >= 0.0


def test_record_lineage_is_not_silently_downgraded(accept_plan, policy_set, catalog) -> None:
    """A record requirement must fail until row provenance is implemented."""

    plan = _lineage_plan(accept_plan, policy_set, catalog, "record")

    with pytest.raises(LineageInstrumentationError, match="record-level"):
        capture_source_lineage(
            plan,
            execution_id="execution-2",
            result_id="sha256:result-2",
        )

    assert ObligationType.LINEAGE_CAPTURE in plan.pending_obligations
