"""Validate approved physical-plan DAGs and dependency-aware event order.

These checks are deliberately structural. They prove that an execution
certificate respects the approved physical-plan skeleton, but they do not
recompute database operator outputs.
"""

from __future__ import annotations

from trustaero.ir.enums import ReasonCode
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    Diagnostic,
    ExecutionEvent,
    PhysicalOperatorSpec,
)


def _diagnostic(code: ReasonCode, message: str, **details: object) -> Diagnostic:
    """Build a machine-classified diagnostic with optional structured details."""

    return Diagnostic(code=code, message=message, details=details)


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


def physical_operators_by_id(
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


def validate_physical_plan_dag(
    physical_plan: ApprovedPhysicalPlan,
) -> tuple[Diagnostic, ...]:
    """Validate the approved physical plan as a standalone operator DAG.

    The logical plan was already checked earlier, but the approved physical plan
    is a separate object. This checker therefore fails closed if the physical
    operator graph has unknown inputs, cycles, or dead branches.
    """

    operators, diagnostics_tuple = physical_operators_by_id(physical_plan)
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


def validate_operator_dependency_events(
    physical_plan: ApprovedPhysicalPlan,
    events: tuple[ExecutionEvent, ...],
) -> tuple[Diagnostic, ...]:
    """Ensure an operator starts only after all direct inputs complete."""

    diagnostics: list[Diagnostic] = []
    operators, operator_diagnostics = physical_operators_by_id(physical_plan)
    if operator_diagnostics:
        return operator_diagnostics

    started_at = _event_index(events, "OperatorStarted")
    completed_at = _event_index(events, "OperatorCompleted")

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
