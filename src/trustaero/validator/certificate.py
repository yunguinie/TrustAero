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
    ExecutionEvent,
    GovernedExecutionCertificate,
    PhysicalOperatorSpec,
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


def _first_sequence(
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


def _event_index(
    events: tuple[ExecutionEvent, ...],
    event_type: str,
) -> dict[str, int]:
    """Index the first sequence number for operator-scoped events."""

    result: dict[str, int] = {}
    for event in events:
        if event.event_type == event_type and event.operator_id is not None:
            result.setdefault(event.operator_id, event.sequence)
    return result


def _physical_operators_by_id(
    physical_plan: ApprovedPhysicalPlan,
) -> tuple[dict[str, PhysicalOperatorSpec], tuple[Diagnostic, ...]]:
    """Build an operator map while rejecting duplicate physical operator IDs."""

    operators: dict[str, PhysicalOperatorSpec] = {}
    diagnostics: list[Diagnostic] = []
    for operator in physical_plan.physical_operators:
        operator_id = operator.physical_operator_id
        if operator_id in operators:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_PHYSICAL_OPERATOR_DUPLICATE,
                    "Approved physical plan contains duplicate physical operator IDs.",
                    operator_id=operator_id,
                )
            )
        operators[operator_id] = operator
    return operators, tuple(diagnostics)


def _validate_physical_plan_dag(
    physical_plan: ApprovedPhysicalPlan,
) -> tuple[Diagnostic, ...]:
    """Validate the approved physical plan as a standalone operator DAG."""

    operators, diagnostics_tuple = _physical_operators_by_id(physical_plan)
    diagnostics = list(diagnostics_tuple)
    known_ids = set(operators)

    for operator in physical_plan.physical_operators:
        for dependency_id in operator.inputs:
            if dependency_id not in known_ids:
                diagnostics.append(
                    _diagnostic(
                        ReasonCode.CERTIFICATE_PHYSICAL_OPERATOR_UNKNOWN,
                        "Physical operator input references an unknown operator.",
                        operator_id=operator.physical_operator_id,
                        dependency_id=dependency_id,
                    )
                )

    if physical_plan.output_operator not in known_ids:
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_PHYSICAL_OPERATOR_UNKNOWN,
                "Approved physical plan output operator is unknown.",
                output_operator=physical_plan.output_operator,
            )
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(operator_id: str, path: tuple[str, ...]) -> None:
        if operator_id in visiting:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_PHYSICAL_PLAN_CYCLIC,
                    "Approved physical plan contains a dependency cycle.",
                    cycle=(*path, operator_id),
                )
            )
            return
        if operator_id in visited or operator_id not in operators:
            return
        visiting.add(operator_id)
        for dependency_id in operators[operator_id].inputs:
            visit(dependency_id, (*path, operator_id))
        visiting.remove(operator_id)
        visited.add(operator_id)

    for operator_id in operators:
        visit(operator_id, ())

    if physical_plan.output_operator in operators:
        reachable: set[str] = set()

        def collect_inputs(operator_id: str) -> None:
            if operator_id in reachable or operator_id not in operators:
                return
            reachable.add(operator_id)
            for dependency_id in operators[operator_id].inputs:
                collect_inputs(dependency_id)

        collect_inputs(physical_plan.output_operator)
        unreachable = sorted(known_ids - reachable)
        if unreachable:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.CERTIFICATE_PHYSICAL_OPERATOR_UNKNOWN,
                    "Physical operators must contribute to the approved output.",
                    unreachable_operators=unreachable,
                    output_operator=physical_plan.output_operator,
                )
            )

    return tuple(diagnostics)


def _validate_operator_dependency_events(
    physical_plan: ApprovedPhysicalPlan,
    certificate: GovernedExecutionCertificate,
) -> tuple[Diagnostic, ...]:
    """Ensure an operator starts only after all direct inputs complete."""

    diagnostics: list[Diagnostic] = []
    operators, operator_diagnostics = _physical_operators_by_id(physical_plan)
    if operator_diagnostics:
        return operator_diagnostics

    started_at = _event_index(certificate.events, "OperatorStarted")
    completed_at = _event_index(certificate.events, "OperatorCompleted")

    for operator in operators.values():
        operator_started = started_at.get(operator.physical_operator_id)
        if operator_started is None:
            continue
        for dependency_id in operator.inputs:
            dependency_completed = completed_at.get(dependency_id)
            if dependency_completed is None:
                continue
            if dependency_completed >= operator_started:
                diagnostics.append(
                    _diagnostic(
                        ReasonCode.CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION,
                        "Physical operator started before a direct input completed.",
                        operator_id=operator.physical_operator_id,
                        dependency_id=dependency_id,
                        operator_started=operator_started,
                        dependency_completed=dependency_completed,
                    )
                )

    return tuple(diagnostics)


def _validate_event_coverage(
    physical_plan: ApprovedPhysicalPlan,
    certificate: GovernedExecutionCertificate,
) -> tuple[Diagnostic, ...]:
    """Check that certificate events cover the approved physical plan skeleton.

    The check is intentionally structural. It verifies that the certificate
    records a plausible execution timeline for every approved physical
    operator, but it does not recompute operator outputs.
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

    plan_approved = _first_sequence(events, "PlanApproved")
    result_materialized = _first_sequence(events, "ResultMaterialized")
    certificate_emitted = _first_sequence(events, "CertificateEmitted")

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
        started = _first_sequence(events, "OperatorStarted", operator_id)
        completed = _first_sequence(events, "OperatorCompleted", operator_id)
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

    if certificate.lineage_evidence is not None and not _has_event(certificate, "LineageRecorded"):
        diagnostics.append(
            _diagnostic(
                ReasonCode.CERTIFICATE_EVENT_MISSING,
                "Certificate with lineage evidence must include a LineageRecorded event.",
                event_type="LineageRecorded",
            )
        )

    lineage_recorded = _first_sequence(events, "LineageRecorded")
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

    diagnostics.extend(_validate_physical_plan_dag(physical_plan))
    diagnostics.extend(_validate_operator_dependency_events(physical_plan, certificate))

    return tuple(diagnostics)


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
    diagnostics.extend(_validate_event_coverage(physical_plan, certificate))

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
