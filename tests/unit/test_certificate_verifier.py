"""Execution-certificate checks over validated logical plans."""

from __future__ import annotations

import copy
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import LineageLevel, ObligationType, ReasonCode, ValidationStatus
from trustaero.ir.models import (
    ExecutionEvent,
    GovernedExecutionCertificate,
    LineageEvidenceSummary,
    PolicySet,
    ValidatedLogicalPlan,
)
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


def _event(event_type: str, payload_digest: str = "sha256:event") -> ExecutionEvent:
    return ExecutionEvent(sequence=0, event_type=event_type, payload_digest=payload_digest)


def _certificate(plan: ValidatedLogicalPlan) -> GovernedExecutionCertificate:
    return GovernedExecutionCertificate(
        certificate_id="cert-1",
        task_digest=plan.validation.canonical_digest,
        logical_plan_id=plan.logical_plan_id,
        physical_plan_id="phys-1",
        policy_snapshot=plan.bindings.policy_snapshot,
        data_snapshots=plan.bindings.data_snapshots,
        events=(_event("ResultMaterialized"), _event("LineageRecorded", "sha256:lineage")),
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
    certificate = _certificate(plan)

    result = verify_execution_certificate(plan, certificate)

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
    certificate = _certificate(plan).model_copy(update={"logical_plan_id": "wrong-plan"})

    result = verify_execution_certificate(plan, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.CERTIFICATE_BINDING_MISMATCH


def test_certificate_rejects_snapshot_mismatch(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    certificate = _certificate(plan).model_copy(
        update={"data_snapshots": {"critical_facilities": "v1900"}}
    )

    result = verify_execution_certificate(plan, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH


def test_certificate_rejects_missing_lineage_evidence(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    certificate = _certificate(plan).model_copy(update={"lineage_evidence": None})

    result = verify_execution_certificate(plan, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_EVIDENCE_MISSING


def test_certificate_rejects_weak_lineage_evidence(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    weak_evidence = _certificate(plan).lineage_evidence
    assert weak_evidence is not None
    certificate = _certificate(plan).model_copy(
        update={
            "lineage_evidence": weak_evidence.model_copy(
                update={"lineage_level": LineageLevel.SOURCE}
            )
        }
    )

    result = verify_execution_certificate(plan, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_LEVEL_INSUFFICIENT


def test_certificate_rejects_missing_result_digest_or_event(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    plan = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)
    certificate = _certificate(plan).model_copy(update={"result_digest": "", "events": ()})

    result = verify_execution_certificate(plan, certificate)

    assert result.status == CertificateVerificationStatus.REJECT
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        ReasonCode.CERTIFICATE_DIGEST_MISSING,
        ReasonCode.CERTIFICATE_EVENT_MISSING,
    }
