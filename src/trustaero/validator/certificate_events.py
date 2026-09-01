"""Validate structural event coverage in governed execution certificates."""

from __future__ import annotations

from trustaero.ir.enums import ReasonCode
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    Diagnostic,
    ExecutionEvent,
    GovernedExecutionCertificate,
)
from trustaero.validator.physical_dag import (
    validate_operator_dependency_events,
    validate_physical_plan_dag,
)


def _diagnostic(code: ReasonCode, message: str, **details: object) -> Diagnostic:
    """Build a machine-classified diagnostic with optional structured details."""

    return Diagnostic(code=code, message=message, details=details)


def first_sequence(
    events: tuple[ExecutionEvent, ...],
    event_type: str,
    operator_id: str | None = None,
) -> int | None:
    """Return the first matching event sequence, or ``None`` if absent."""

    sequences = [
        event.sequence
        for event in events
        if event.event_type == event_type
        and (operator_id is None or event.operator_id == operator_id)
    ]
    return min(sequences) if sequences else None


def has_event(certificate: GovernedExecutionCertificate, event_type: str) -> bool:
    """Return whether a certificate contains at least one event of this type."""

    return any(event.event_type == event_type for event in certificate.events)


def validate_event_coverage(
    physical_plan: ApprovedPhysicalPlan,
    certificate: GovernedExecutionCertificate,
) -> tuple[Diagnostic, ...]:
    """Check that certificate events cover the approved physical plan skeleton.

    The check is intentionally structural. It verifies that the certificate
    records a plausible execution timeline for every approved physical
    operator, while leaving byte-level result recomputation to a future backend.
    """

    diagnostics: list[Diagnostic] = []
    events = certificate.events
    sequences = [event.sequence for event in events]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                "Certificate event sequences must be unique and monotonically increasing.",
                sequences=sequences,
            )
        )

    plan_approved = first_sequence(events, "PlanApproved")
    result_materialized = first_sequence(events, "ResultMaterialized")
    certificate_emitted = first_sequence(events, "CertificateEmitted")

    if plan_approved is None:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_MISSING,
                "Certificate must include a PlanApproved event.",
                event_type="PlanApproved",
            )
        )
    if certificate_emitted is None:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_MISSING,
                "Certificate must include a CertificateEmitted event.",
                event_type="CertificateEmitted",
            )
        )

    completed_sequences: list[int] = []
    for operator in physical_plan.physical_operators:
        operator_id = operator.physical_operator_id
        started = first_sequence(events, "OperatorStarted", operator_id)
        completed = first_sequence(events, "OperatorCompleted", operator_id)
        if started is None:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_OPERATOR_EVENT_MISSING,
                    "Certificate is missing OperatorStarted for a physical operator.",
                    operator_id=operator_id,
                    event_type="OperatorStarted",
                )
            )
        if completed is None:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_OPERATOR_EVENT_MISSING,
                    "Certificate is missing OperatorCompleted for a physical operator.",
                    operator_id=operator_id,
                    event_type="OperatorCompleted",
                )
            )
        if started is not None and completed is not None:
            completed_sequences.append(completed)
            if completed <= started:
                diagnostics.append(
                    _diagnostic(
                        ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                        "OperatorCompleted must occur after OperatorStarted.",
                        operator_id=operator_id,
                        started=started,
                        completed=completed,
                    )
                )
            if plan_approved is not None and started <= plan_approved:
                diagnostics.append(
                    _diagnostic(
                        ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                        "OperatorStarted must occur after PlanApproved.",
                        operator_id=operator_id,
                        plan_approved=plan_approved,
                        started=started,
                    )
                )

    if result_materialized is not None and completed_sequences:
        latest_completed = max(completed_sequences)
        if result_materialized <= latest_completed:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                    "ResultMaterialized must occur after all OperatorCompleted events.",
                    result_materialized=result_materialized,
                    latest_operator_completed=latest_completed,
                )
            )

    if certificate.lineage_evidence is not None and not has_event(certificate, "LineageRecorded"):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_MISSING,
                "Certificate with lineage evidence must include a LineageRecorded event.",
                event_type="LineageRecorded",
            )
        )

    lineage_recorded = first_sequence(events, "LineageRecorded")
    if (
        lineage_recorded is not None
        and result_materialized is not None
        and lineage_recorded <= result_materialized
    ):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                "LineageRecorded must occur after ResultMaterialized in IR v1 certificates.",
                lineage_recorded=lineage_recorded,
                result_materialized=result_materialized,
            )
        )

    if (
        certificate_emitted is not None
        and result_materialized is not None
        and certificate_emitted <= result_materialized
    ):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                "CertificateEmitted must occur after ResultMaterialized.",
                certificate_emitted=certificate_emitted,
                result_materialized=result_materialized,
            )
        )
    if (
        certificate_emitted is not None
        and lineage_recorded is not None
        and certificate_emitted <= lineage_recorded
    ):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_ORDER_INVALID,
                "CertificateEmitted must occur after LineageRecorded.",
                certificate_emitted=certificate_emitted,
                lineage_recorded=lineage_recorded,
            )
        )

    diagnostics.extend(validate_physical_plan_dag(physical_plan))
    diagnostics.extend(validate_operator_dependency_events(physical_plan, certificate.events))

    return tuple(diagnostics)
