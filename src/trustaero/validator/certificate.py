"""Verify governed execution certificates against approved TrustAero plans.

This module is the certificate-verification entry point. It delegates structural
event and physical-DAG checks to smaller modules so each checker has a clear
semantic boundary.
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
from trustaero.validator.certificate_events import (
    first_sequence,
    has_event,
    validate_event_coverage,
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
    """Build a machine-classified diagnostic with optional structured details."""

    return Diagnostic(code=code, message=message, details=details)


def _non_empty_digest(value: str) -> bool:
    """Return whether a digest-shaped binding claim is present."""

    return bool(value) and ":" in value


def _result_event_digest(certificate: GovernedExecutionCertificate) -> str | None:
    """Return the first ResultMaterialized event digest, if the event exists."""

    result_sequence = first_sequence(certificate.events, "ResultMaterialized")
    if result_sequence is None:
        return None
    for event in certificate.events:
        if event.sequence == result_sequence:
            return event.payload_digest
    return None


def verify_execution_certificate(
    plan: ValidatedLogicalPlan,
    physical_plan: ApprovedPhysicalPlan,
    certificate: GovernedExecutionCertificate,
    observed_result_digest: str | None = None,
) -> CertificateVerification:
    """Check logical/physical bindings, snapshots, evidence, and digests.

    ``observed_result_digest`` is an independently computed digest from a
    trusted executor. Without it, the certificate's ``result_digest`` is only a
    binding claim and remains listed as unverified instead of self-proving.
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
    result_event_digest = _result_event_digest(certificate)
    if result_event_digest is not None and result_event_digest != certificate.result_digest:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_RESULT_DIGEST_MISMATCH,
                "ResultMaterialized event digest does not match certificate result_digest.",
                expected=certificate.result_digest,
                actual=result_event_digest,
            )
        )
    if (
        observed_result_digest is not None
        and _non_empty_digest(certificate.result_digest)
        and observed_result_digest != certificate.result_digest
    ):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_RESULT_DIGEST_MISMATCH,
                "Observed trusted-executor result digest does not match the certificate.",
                expected=certificate.result_digest,
                actual=observed_result_digest,
            )
        )
    if not has_event(certificate, "ResultMaterialized"):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_MISSING,
                "Certificate must include a ResultMaterialized event.",
                event_type="ResultMaterialized",
            )
        )
    diagnostics.extend(validate_event_coverage(physical_plan, certificate))

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
        # Even when the result digest is independently checked, the physical
        # execution trace is still trusted-executor evidence rather than a
        # cryptographic proof against a malicious DBMS.
        status = CertificateVerificationStatus.PARTIAL

    unverified_components = ["physical_plan_execution"]
    if observed_result_digest is None:
        unverified_components.insert(0, "result_content_digest")

    return CertificateVerification(
        status=status,
        verified_obligations=tuple(dict.fromkeys(verified)),
        diagnostics=tuple(diagnostics),
        unverified_components=tuple(unverified_components),
    )
