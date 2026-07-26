"""Phase 0 fault-injection tests for planner/certificate bindings."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.experiments.runner import _certificate_inputs
from trustaero.ir.enums import ReasonCode, ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)
from trustaero.validator.service import validate


def _validated_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> ValidatedLogicalPlan:
    response = validate(copy.deepcopy(rewrite_plan), policy_set, catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    return response.validated_plan


def _verify(plan: ValidatedLogicalPlan, scenario: str):
    inputs = _certificate_inputs(plan, scenario)
    return verify_execution_certificate(
        plan,
        inputs.physical_plan,
        inputs.certificate,
        observed_planner_decision_digest=inputs.observed_planner_digest,
        observed_planner_selected_candidate_id=(inputs.observed_planner_candidate_id),
    )


def test_valid_planner_binding_remains_partial_not_self_proving(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_plan(rewrite_plan, policy_set, catalog)

    result = _verify(plan, "planner_binding_valid")

    assert result.status == CertificateVerificationStatus.PARTIAL
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    ("scenario", "reason_code"),
    (
        (
            "planner_digest_mismatch",
            ReasonCode.CERTIFICATE_PLANNER_DECISION_MISMATCH,
        ),
        (
            "planner_candidate_mismatch",
            ReasonCode.CERTIFICATE_PLANNER_DECISION_MISMATCH,
        ),
        (
            "planner_strategy_mismatch",
            ReasonCode.PHYSICAL_PLAN_PLANNER_DECISION_MISMATCH,
        ),
        (
            "planner_binding_missing",
            ReasonCode.CERTIFICATE_PLANNER_DECISION_MISMATCH,
        ),
        (
            "planner_observation_mismatch",
            ReasonCode.PHYSICAL_PLAN_PLANNER_DECISION_MISMATCH,
        ),
    ),
)
def test_planner_binding_faults_are_rejected_with_stable_codes(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
    scenario: str,
    reason_code: ReasonCode,
) -> None:
    plan = _validated_plan(rewrite_plan, policy_set, catalog)

    result = _verify(plan, scenario)

    assert result.status == CertificateVerificationStatus.REJECT
    assert reason_code in {diagnostic.code for diagnostic in result.diagnostics}
