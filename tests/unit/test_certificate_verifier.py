"""Execution-certificate checks over validated logical plans."""

from __future__ import annotations

import copy
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import LineageLevel, ObligationType, ReasonCode, ValidationStatus
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    ExecutionEvent,
    GovernedExecutionCertificate,
    LineageEvidenceSummary,
    PolicySet,
    ValidatedLogicalPlan,
)
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)
from trustaero.validator.service import validate


def _validated_rewrite_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> ValidatedLogicalPlan:
    response = validate(copy.deepcopy(rewrite_plan), policy_set, catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    return response.validated_plan


def _event(
    sequence: int,
    event_type: str,
    payload_digest: str = "sha256:event",
    operator_id: str | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        sequence=sequence,
        event_type=event_type,
        operator_id=operator_id,
        payload_digest=payload_digest,
    )


def _events_for_physical_plan(physical: ApprovedPhysicalPlan) -> tuple[ExecutionEvent, ...]:
    """Build a complete structural event trace for the approved physical plan."""

    events: list[ExecutionEvent] = [_event(0, "PlanApproved", physical.physical_plan_id)]
    sequence = 1
    for operator in physical.physical_operators:
        events.append(
            _event(
                sequence,
                "OperatorStarted",
                f"sha256:start-{operator.physical_operator_id}",
                operator.physical_operator_id,
            )
        )
        sequence += 1
        events.append(
            _event(
                sequence,
                "OperatorCompleted",
                f"sha256:done-{operator.physical_operator_id}",
                operator.physical_operator_id,
            )
        )
        sequence += 1
    events.append(_event(sequence, "ResultMaterialized", "sha256:result"))
    sequence += 1
    events.append(_event(sequence, "LineageRecorded", "sha256:lineage"))
    sequence += 1
    events.append(_event(sequence, "CertificateEmitted", "sha256:certificate"))
    return tuple(events)


def _certificate(plan: ValidatedLogicalPlan) -> GovernedExecutionCertificate:
    physical = plan_physical_execution(plan)
    return GovernedExecutionCertificate(
        certificate_id="cert-1",
        task_digest=plan.validation.canonical_digest,
        logical_plan_id=plan.logical_plan_id,
        physical_plan_id=physical.physical_plan_id,
        policy_snapshot=plan.bindings.policy_snapshot,
        data_snapshots=plan.bindings.data_snapshots,
        events=_events_for_physical_plan(physical),
        result_digest="sha256:result",
        lineage_digest="sha256:lineage",
        lineage_evidence=LineageEvidenceSummary(
            execution_id="exec-1",
            result_id="result-1",
            lineage_level=LineageLevel.RECORD,
            covered_operators=(plan.lineage_requirements[0].target_operator,),
            edge_digest="sha256:edges",
        ),
    )


def test_certificate_verifies_bindings_and_upgrades_lineage_pending_obligation(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.PARTIAL
    assert ObligationType.LINEAGE_CAPTURE in result.verified_obligations
    assert result.diagnostics == ()
    assert result.unverified_components == (
        "result_content_digest",
        "physical_plan_execution",
    )


def test_certificate_rejects_logical_plan_binding_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan).model_copy(update={"logical_plan_id": "wrong-plan"})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.CERTIFICATE_BINDING_MISMATCH


def test_certificate_rejects_physical_plan_binding_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan).model_copy(update={"physical_plan_id": "wrong-phys"})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH


def test_certificate_rejects_unbound_approved_physical_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan).model_copy(update={"logical_plan_id": "other-plan"})
    assert isinstance(physical, ApprovedPhysicalPlan)
    certificate = _certificate(plan)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH


def test_certificate_rejects_snapshot_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan).model_copy(
        update={"data_snapshots": {"critical_facilities": "v1900"}}
    )

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH


def test_certificate_rejects_missing_lineage_evidence(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan).model_copy(update={"lineage_evidence": None})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_EVIDENCE_MISSING


def test_certificate_rejects_weak_lineage_evidence(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    weak_evidence = _certificate(plan).lineage_evidence
    assert weak_evidence is not None
    certificate = _certificate(plan).model_copy(
        update={
            "lineage_evidence": weak_evidence.model_copy(
                update={"lineage_level": LineageLevel.SOURCE}
            )
        }
    )

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_LEVEL_INSUFFICIENT


def test_certificate_rejects_missing_result_digest_or_event(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan).model_copy(update={"result_digest": "", "events": ()})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        ReasonCode.CERTIFICATE_DIGEST_MISSING,
        ReasonCode.CERTIFICATE_EVENT_MISSING,
    }


def test_certificate_rejects_missing_operator_event(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    missing_operator = physical.physical_operators[0].physical_operator_id
    events = tuple(
        event
        for event in _certificate(plan).events
        if not (event.event_type == "OperatorCompleted" and event.operator_id == missing_operator)
    )
    certificate = _certificate(plan).model_copy(update={"events": events})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_OPERATOR_EVENT_MISSING
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_result_before_operator_completion(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    events = tuple(
        event.model_copy(update={"sequence": 1})
        if event.event_type == "ResultMaterialized"
        else event
        for event in _certificate(plan).events
    )
    certificate = _certificate(plan).model_copy(update={"events": events})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_lineage_evidence_without_lineage_event(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    events = tuple(
        event for event in _certificate(plan).events if event.event_type != "LineageRecorded"
    )
    certificate = _certificate(plan).model_copy(update={"events": events})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_EVENT_MISSING
        and diagnostic.details["event_type"] == "LineageRecorded"
        for diagnostic in result.diagnostics
    )
