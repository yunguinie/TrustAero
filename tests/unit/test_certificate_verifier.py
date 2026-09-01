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
    PhysicalOperatorSpec,
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


def _physical_plan_from_edges(
    plan: ValidatedLogicalPlan,
    edges: dict[str, tuple[str, ...]],
    output_operator: str,
    physical_plan_id: str = "pp-test",
) -> ApprovedPhysicalPlan:
    """Create a small physical DAG for certificate dependency tests."""

    return ApprovedPhysicalPlan(
        physical_plan_id=physical_plan_id,
        logical_plan_id=plan.logical_plan_id,
        logical_plan_digest=plan.validation.canonical_digest,
        output_operator=output_operator,
        physical_operators=tuple(
            PhysicalOperatorSpec(
                physical_operator_id=operator_id,
                logical_operator_id=f"logical-{operator_id}",
                operator_type="Synthetic",
                inputs=inputs,
            )
            for operator_id, inputs in edges.items()
        ),
        bindings=plan.bindings,
        lineage_instrumentation=plan.lineage_instrumentation,
        pending_obligations=plan.pending_obligations,
    )


def _certificate(
    plan: ValidatedLogicalPlan,
    physical: ApprovedPhysicalPlan | None = None,
) -> GovernedExecutionCertificate:
    physical = plan_physical_execution(plan) if physical is None else physical
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


def _certificate_with_events(
    plan: ValidatedLogicalPlan,
    physical: ApprovedPhysicalPlan,
    events: tuple[ExecutionEvent, ...],
) -> GovernedExecutionCertificate:
    return _certificate(plan, physical).model_copy(update={"events": events})


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


def test_certificate_with_observed_result_digest_verifies_result_binding(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan)

    result = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest=certificate.result_digest,
    )

    assert result.status == CertificateVerificationStatus.PARTIAL
    assert result.diagnostics == ()
    assert result.unverified_components == ("physical_plan_execution",)


def test_certificate_binds_independently_recomputed_planner_decision(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """A copied digest is accepted only when trusted recomputation agrees."""

    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    base = plan_physical_execution(plan)
    selected = base.strategy.strategy_id
    digest = "sha256:planner-decision"
    physical = base.model_copy(
        update={
            "planner_decision_digest": digest,
            "planner_selected_candidate_id": selected,
        }
    )
    certificate = _certificate(plan, physical).model_copy(
        update={
            "planner_decision_digest": digest,
            "planner_selected_candidate_id": selected,
        }
    )

    result = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_planner_decision_digest=digest,
        observed_planner_selected_candidate_id=selected,
    )

    assert result.status == CertificateVerificationStatus.PARTIAL
    assert result.diagnostics == ()
    assert "planner_decision" not in result.unverified_components


def test_unobserved_planner_digest_remains_explicitly_unverified(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    base = plan_physical_execution(plan)
    selected = base.strategy.strategy_id
    physical = base.model_copy(
        update={
            "planner_decision_digest": "sha256:planner-decision",
            "planner_selected_candidate_id": selected,
        }
    )
    certificate = _certificate(plan, physical).model_copy(
        update={
            "planner_decision_digest": "sha256:planner-decision",
            "planner_selected_candidate_id": selected,
        }
    )

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.PARTIAL
    assert result.diagnostics == ()
    assert "planner_decision" in result.unverified_components


def test_certificate_rejects_planner_decision_binding_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    base = plan_physical_execution(plan)
    selected = base.strategy.strategy_id
    physical = base.model_copy(
        update={
            "planner_decision_digest": "sha256:expected",
            "planner_selected_candidate_id": selected,
        }
    )
    certificate = _certificate(plan, physical).model_copy(
        update={
            "planner_decision_digest": "sha256:tampered",
            "planner_selected_candidate_id": selected,
        }
    )

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert ReasonCode.CERTIFICATE_PLANNER_DECISION_MISMATCH in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_certificate_rejects_observed_result_digest_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan)

    result = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest="sha256:other-result",
    )

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_RESULT_DIGEST_MISMATCH
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_result_event_digest_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    events = tuple(
        event.model_copy(update={"payload_digest": "sha256:other-result"})
        if event.event_type == "ResultMaterialized"
        else event
        for event in _certificate(plan).events
    )
    certificate = _certificate(plan).model_copy(update={"events": events})

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_RESULT_DIGEST_MISMATCH
        for diagnostic in result.diagnostics
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


def test_certificate_allows_interleaved_independent_branches_before_join(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = _physical_plan_from_edges(
        plan,
        {
            "phys-scan-a": (),
            "phys-scan-b": (),
            "phys-join": ("phys-scan-a", "phys-scan-b"),
        },
        "phys-join",
    )
    events = (
        _event(0, "PlanApproved", physical.physical_plan_id),
        _event(1, "OperatorStarted", "sha256:start-a", "phys-scan-a"),
        _event(2, "OperatorStarted", "sha256:start-b", "phys-scan-b"),
        _event(3, "OperatorCompleted", "sha256:done-a", "phys-scan-a"),
        _event(4, "OperatorCompleted", "sha256:done-b", "phys-scan-b"),
        _event(5, "OperatorStarted", "sha256:start-join", "phys-join"),
        _event(6, "OperatorCompleted", "sha256:done-join", "phys-join"),
        _event(7, "ResultMaterialized", "sha256:result"),
        _event(8, "LineageRecorded", "sha256:lineage"),
        _event(9, "CertificateEmitted", "sha256:certificate"),
    )
    certificate = _certificate_with_events(plan, physical, events)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.PARTIAL
    assert result.diagnostics == ()


def test_certificate_rejects_linear_dependency_violation(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = _physical_plan_from_edges(
        plan,
        {
            "phys-scan": (),
            "phys-filter": ("phys-scan",),
            "phys-project": ("phys-filter",),
        },
        "phys-project",
    )
    events = (
        _event(0, "PlanApproved", physical.physical_plan_id),
        _event(1, "OperatorStarted", "sha256:start-filter", "phys-filter"),
        _event(2, "OperatorStarted", "sha256:start-scan", "phys-scan"),
        _event(3, "OperatorCompleted", "sha256:done-scan", "phys-scan"),
        _event(4, "OperatorCompleted", "sha256:done-filter", "phys-filter"),
        _event(5, "OperatorStarted", "sha256:start-project", "phys-project"),
        _event(6, "OperatorCompleted", "sha256:done-project", "phys-project"),
        _event(7, "ResultMaterialized", "sha256:result"),
        _event(8, "LineageRecorded", "sha256:lineage"),
        _event(9, "CertificateEmitted", "sha256:certificate"),
    )
    certificate = _certificate_with_events(plan, physical, events)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION
        and diagnostic.details["operator_id"] == "phys-filter"
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_join_before_all_inputs_complete(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = _physical_plan_from_edges(
        plan,
        {
            "phys-scan-a": (),
            "phys-scan-b": (),
            "phys-join": ("phys-scan-a", "phys-scan-b"),
        },
        "phys-join",
    )
    events = (
        _event(0, "PlanApproved", physical.physical_plan_id),
        _event(1, "OperatorStarted", "sha256:start-a", "phys-scan-a"),
        _event(2, "OperatorCompleted", "sha256:done-a", "phys-scan-a"),
        _event(3, "OperatorStarted", "sha256:start-b", "phys-scan-b"),
        _event(4, "OperatorStarted", "sha256:start-join", "phys-join"),
        _event(5, "OperatorCompleted", "sha256:done-b", "phys-scan-b"),
        _event(6, "OperatorCompleted", "sha256:done-join", "phys-join"),
        _event(7, "ResultMaterialized", "sha256:result"),
        _event(8, "LineageRecorded", "sha256:lineage"),
        _event(9, "CertificateEmitted", "sha256:certificate"),
    )
    certificate = _certificate_with_events(plan, physical, events)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION
        and diagnostic.details["dependency_id"] == "phys-scan-b"
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_multilevel_dependency_violation(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = _physical_plan_from_edges(
        plan,
        {
            "phys-scan-a": (),
            "phys-filter-a": ("phys-scan-a",),
            "phys-scan-b": (),
            "phys-filter-b": ("phys-scan-b",),
            "phys-join": ("phys-filter-a", "phys-filter-b"),
        },
        "phys-join",
    )
    events = (
        _event(0, "PlanApproved", physical.physical_plan_id),
        _event(1, "OperatorStarted", "sha256:start-scan-a", "phys-scan-a"),
        _event(2, "OperatorCompleted", "sha256:done-scan-a", "phys-scan-a"),
        _event(3, "OperatorStarted", "sha256:start-filter-a", "phys-filter-a"),
        _event(4, "OperatorCompleted", "sha256:done-filter-a", "phys-filter-a"),
        _event(5, "OperatorStarted", "sha256:start-scan-b", "phys-scan-b"),
        _event(6, "OperatorCompleted", "sha256:done-scan-b", "phys-scan-b"),
        _event(7, "OperatorStarted", "sha256:start-filter-b", "phys-filter-b"),
        _event(8, "OperatorStarted", "sha256:start-join", "phys-join"),
        _event(9, "OperatorCompleted", "sha256:done-filter-b", "phys-filter-b"),
        _event(10, "OperatorCompleted", "sha256:done-join", "phys-join"),
        _event(11, "ResultMaterialized", "sha256:result"),
        _event(12, "LineageRecorded", "sha256:lineage"),
        _event(13, "CertificateEmitted", "sha256:certificate"),
    )
    certificate = _certificate_with_events(plan, physical, events)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION
        and diagnostic.details["dependency_id"] == "phys-filter-b"
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_unknown_physical_operator_input(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = _physical_plan_from_edges(
        plan,
        {"phys-filter": ("phys-missing",)},
        "phys-filter",
    )
    certificate = _certificate(plan, physical)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_PHYSICAL_OPERATOR_UNKNOWN
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_lineage_event_digest_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """Lineage evidence must bind to the corresponding execution event."""

    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan, physical).model_copy(
        update={"lineage_digest": "sha256:forged-lineage"}
    )

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT
        for diagnostic in result.diagnostics
    )


def test_certificate_rejects_cyclic_physical_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    physical = _physical_plan_from_edges(
        plan,
        {
            "phys-a": ("phys-b",),
            "phys-b": ("phys-a",),
        },
        "phys-a",
    )
    certificate = _certificate(plan, physical)

    result = verify_execution_certificate(plan, physical, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert any(
        diagnostic.code == ReasonCode.CERTIFICATE_PHYSICAL_PLAN_CYCLIC
        for diagnostic in result.diagnostics
    )
