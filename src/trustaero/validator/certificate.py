"""Verify governed execution certificates against approved TrustAero plans.

This checker validates certificate structure and bindings that can be checked
without a real database executor. It deliberately marks result contents as
unverified until a future backend can recompute physical execution digests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trustaero.ir.enums import ObligationType, ReasonCode
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    Diagnostic,
    GovernedExecutionCertificate,
    ValidatedLogicalPlan,
)
from trustaero.validator.lineage import verify_lineage_evidence


class CertificateVerificationStatus(StrEnum):
    """Outcome for certificate checks over the current bounded fragment."""

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    REJECT = "REJECT"


@dataclass(frozen=True)
class CertificateVerification:
    """Certificate obligations proven by independent checks and root failures."""

    status: CertificateVerificationStatus
    verified_obligations: tuple[ObligationType, ...]
    diagnostics: tuple[Diagnostic, ...]
    unverified_components: tuple[str, ...] = ()


def _diagnostic(code: ReasonCode, message: str, **details: object) -> Diagnostic:
    return Diagnostic(code=code, message=message, details=details)


def _non_empty_digest(value: str) -> bool:
    return bool(value) and ":" in value


def _has_event(certificate: GovernedExecutionCertificate, event_type: str) -> bool:
    return any(event.event_type == event_type for event in certificate.events)


def verify_execution_certificate(
    plan: ValidatedLogicalPlan,
    physical_plan: ApprovedPhysicalPlan,
    certificate: GovernedExecutionCertificate,
) -> CertificateVerification:
    """Check logical/physical bindings, snapshots, evidence, and digests.

    Current V1 does not recompute database result bytes or physical-plan
    execution. Those parts are listed in ``unverified_components`` instead of
    being treated as silently proven.
    """

    diagnostics: list[Diagnostic] = []
    verified = list(plan.satisfied_obligations)

    if physical_plan.logical_plan_id != plan.logical_plan_id:
        diagnostics.append(
            _diagnostic(
                ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH,
                "Approved physical plan does not bind to the validated logical plan.",
                expected=plan.logical_plan_id,
                actual=physical_plan.logical_plan_id,
            )
        )
    if physical_plan.logical_plan_digest != plan.validation.canonical_digest:
        diagnostics.append(
            _diagnostic(
                ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH,
                "Approved physical plan digest does not match the validated plan digest.",
                expected=plan.validation.canonical_digest,
                actual=physical_plan.logical_plan_digest,
            )
        )
    if physical_plan.bindings != plan.bindings:
        diagnostics.append(
            _diagnostic(
                ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH,
                "Approved physical plan snapshot bindings do not match the validated plan.",
                expected=plan.bindings.model_dump(mode="json"),
                actual=physical_plan.bindings.model_dump(mode="json"),
            )
        )
    if physical_plan.lineage_instrumentation != plan.lineage_instrumentation:
        diagnostics.append(
            _diagnostic(
                ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH,
                "Approved physical plan lineage instrumentation diverges from validation.",
            )
        )
    if physical_plan.pending_obligations != plan.pending_obligations:
        diagnostics.append(
            _diagnostic(
                ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH,
                "Approved physical plan pending obligations diverge from validation.",
                expected=[obligation.value for obligation in plan.pending_obligations],
                actual=[obligation.value for obligation in physical_plan.pending_obligations],
            )
        )

    if certificate.logical_plan_id != plan.logical_plan_id:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_BINDING_MISMATCH,
                "Certificate logical_plan_id does not match the validated plan.",
                expected=plan.logical_plan_id,
                actual=certificate.logical_plan_id,
            )
        )
    if certificate.task_digest != plan.validation.canonical_digest:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_BINDING_MISMATCH,
                "Certificate task_digest does not bind to the validated plan digest.",
                expected=plan.validation.canonical_digest,
                actual=certificate.task_digest,
            )
        )
    if certificate.physical_plan_id != physical_plan.physical_plan_id:
        diagnostics.append(
            _diagnostic(
                ReasonCode.PHYSICAL_PLAN_BINDING_MISMATCH,
                "Certificate physical_plan_id does not match the approved physical plan.",
                expected=physical_plan.physical_plan_id,
                actual=certificate.physical_plan_id,
            )
        )
    if certificate.policy_snapshot != plan.bindings.policy_snapshot:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH,
                "Certificate policy snapshot does not match the validated plan.",
                expected=plan.bindings.policy_snapshot,
                actual=certificate.policy_snapshot,
            )
        )
    if certificate.data_snapshots != plan.bindings.data_snapshots:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH,
                "Certificate data snapshots do not match the validated plan.",
                expected=plan.bindings.data_snapshots,
                actual=certificate.data_snapshots,
            )
        )
    if not _non_empty_digest(certificate.result_digest):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_DIGEST_MISSING,
                "Certificate must include a non-empty result digest.",
                field="result_digest",
            )
        )
    if not _has_event(certificate, "ResultMaterialized"):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_MISSING,
                "Certificate must include a ResultMaterialized event.",
                event_type="ResultMaterialized",
            )
        )

    lineage_check = verify_lineage_evidence(
        plan.lineage_requirements,
        plan.lineage_instrumentation,
        certificate.lineage_evidence,
    )
    diagnostics.extend(lineage_check.diagnostics)
    if lineage_check.satisfied and ObligationType.LINEAGE_CAPTURE in plan.pending_obligations:
        verified.append(ObligationType.LINEAGE_CAPTURE)
        if not certificate.lineage_digest:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_DIGEST_MISSING,
                    "Certificate must include lineage_digest when lineage is verified.",
                    field="lineage_digest",
                )
            )

    if diagnostics:
        status = CertificateVerificationStatus.REJECT
    else:
        # The executor has not been integrated, so content digests are binding
        # claims only. They are not independently recomputed in this phase.
        status = CertificateVerificationStatus.PARTIAL

    return CertificateVerification(
        status=status,
        verified_obligations=tuple(dict.fromkeys(verified)),
        diagnostics=tuple(diagnostics),
        unverified_components=("result_content_digest", "physical_plan_execution"),
    )
