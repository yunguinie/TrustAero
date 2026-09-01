"""Independent postcondition checks for governance obligation rewrites.

The rewriter is not allowed to certify its own output merely because it
constructed an operator. This module checks the resulting output suffix and
snapshot bindings against the policy obligations using only public IR state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trustaero.ir.enums import LineageLevel, ObligationType, ReasonCode
from trustaero.ir.models import (
    Diagnostic,
    GeneralizeLocation,
    LineageCapture,
    LineageInstrumentationSpec,
    LineageRequirement,
    Mask,
    MinGroupSize,
    Obligation,
    Operator,
    ScanSource,
)


@dataclass(frozen=True)
class ObligationVerification:
    """Obligations proven by the validated suffix and any root failures."""

    satisfied: tuple[ObligationType, ...]
    pending: tuple[ObligationType, ...]
    lineage_requirements: tuple[LineageRequirement, ...]
    lineage_instrumentation: tuple[LineageInstrumentationSpec, ...]
    diagnostics: tuple[Diagnostic, ...]


def _failure(message: str, **details: Any) -> Diagnostic:
    return Diagnostic(
        code=ReasonCode.OBLIGATION_NOT_ENFORCED,
        message=message,
        details=details,
    )


def _enforcement_suffix(
    operators: tuple[Operator, ...],
    boundary_operator: str,
    output_operator: str,
) -> tuple[tuple[Operator, ...], Diagnostic | None]:
    """Walk from the rewritten output back to the original output boundary.

    Only unary operators after ``boundary_operator`` may count as governance
    enforcement. An operator elsewhere in the candidate graph cannot satisfy a
    post-output obligation by coincidence.
    """

    by_id = {operator.operator_id: operator for operator in operators}
    if boundary_operator not in by_id or output_operator not in by_id:
        return (), _failure(
            "Governance boundary or rewritten output does not exist.",
            boundary_operator=boundary_operator,
            output_operator=output_operator,
        )
    suffix: list[Operator] = []
    visited: set[str] = set()
    current = output_operator

    while current != boundary_operator:
        if current in visited:
            return (), _failure(
                "Governance output suffix contains a cycle.",
                boundary_operator=boundary_operator,
                output_operator=output_operator,
            )
        visited.add(current)
        operator = by_id.get(current)
        if operator is None or len(operator.inputs) != 1:
            return (), _failure(
                "Rewritten output is not a unary enforcement chain after the candidate output.",
                boundary_operator=boundary_operator,
                output_operator=output_operator,
                stopped_at=current,
            )
        suffix.append(operator)
        current = operator.inputs[0]

    return tuple(suffix), None


def _string_set(value: Any) -> frozenset[str] | None:
    """Parse a non-empty JSON sequence of field names without coercion."""

    if not isinstance(value, (list, tuple)) or not value:
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    return frozenset(value)


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        return None
    return int(value)


def _lineage_strength(level: LineageLevel) -> int:
    return {
        LineageLevel.NONE: 0,
        LineageLevel.SOURCE: 1,
        LineageLevel.RECORD: 2,
    }[level]


def _lineage_requirement(
    obligation: Obligation, boundary_operator: str
) -> tuple[LineageRequirement | None, Diagnostic | None]:
    raw_level = obligation.parameters.get("level")
    if not isinstance(raw_level, str):
        return None, _failure(
            "Lineage obligation has an invalid required level.",
            obligation_type=obligation.obligation_type.value,
            parameters=obligation.parameters,
        )
    try:
        level = LineageLevel(raw_level)
    except ValueError:
        return None, _failure(
            "Lineage obligation has an unsupported required level.",
            obligation_type=obligation.obligation_type.value,
            parameters=obligation.parameters,
        )
    return LineageRequirement(level=level, target_operator=boundary_operator), None


def _lineage_instrumentation(
    requirement: LineageRequirement, suffix: tuple[Operator, ...]
) -> LineageInstrumentationSpec | None:
    for operator in suffix:
        if not isinstance(operator, LineageCapture):
            continue
        if _lineage_strength(operator.level) >= _lineage_strength(requirement.level):
            return LineageInstrumentationSpec(
                level=operator.level,
                target_operator=requirement.target_operator,
                capture_operator=operator.operator_id,
            )
    return None


def _matches_suffix(obligation: Obligation, suffix: tuple[Operator, ...]) -> bool:
    """Return whether a typed suffix operator meets or exceeds a requirement."""

    params = obligation.parameters
    obligation_type = obligation.obligation_type

    if obligation_type == ObligationType.MASK:
        required_fields = _string_set(params.get("fields"))
        method = params.get("method", "redact")
        return required_fields is not None and any(
            isinstance(operator, Mask)
            and required_fields <= frozenset(operator.fields)
            and operator.method == method
            for operator in suffix
        )

    if obligation_type == ObligationType.GENERALIZE_LOCATION:
        required_fields = _string_set(params.get("fields"))
        required_precision = _positive_number(params.get("precision_km"))
        method = params.get("method", "fixed_grid")
        return (
            required_fields is not None
            and required_precision is not None
            and any(
                isinstance(operator, GeneralizeLocation)
                and required_fields <= frozenset(operator.fields)
                # A larger fixed-grid cell discloses no more spatial detail.
                and operator.precision_km >= required_precision
                and operator.method == method
                and operator.preserves_selection is True
                for operator in suffix
            )
        )

    if obligation_type == ObligationType.MIN_GROUP_SIZE:
        required_minimum = _positive_integer(params.get("minimum_count"))
        return required_minimum is not None and any(
            isinstance(operator, MinGroupSize) and operator.minimum_count >= required_minimum
            for operator in suffix
        )

    if obligation_type == ObligationType.LINEAGE_CAPTURE:
        return False

    return False


def verify_obligations(
    obligations: tuple[Obligation, ...],
    operators: tuple[Operator, ...],
    *,
    boundary_operator: str,
    output_operator: str,
    data_snapshots: Mapping[str, str],
) -> ObligationVerification:
    """Prove each policy obligation from the final IR and resolved bindings.

    ``boundary_operator`` is the untrusted candidate's declared output.
    Governance operators count only if they form the unary suffix leading from
    that boundary to the validated plan's final output.
    """

    suffix, boundary_error = _enforcement_suffix(
        operators,
        boundary_operator,
        output_operator,
    )
    if boundary_error is not None:
        return ObligationVerification((), (), (), (), (boundary_error,))

    scan_datasets = {operator.dataset for operator in operators if isinstance(operator, ScanSource)}
    satisfied: list[ObligationType] = []
    pending: list[ObligationType] = []
    lineage_requirements: list[LineageRequirement] = []
    lineage_instrumentation: list[LineageInstrumentationSpec] = []
    diagnostics: list[Diagnostic] = []

    for obligation in obligations:
        obligation_type = obligation.obligation_type
        if obligation_type == ObligationType.VERSION_PIN:
            enforced = (
                not obligation.parameters
                and bool(scan_datasets)
                and all(data_snapshots.get(dataset) for dataset in scan_datasets)
            )
        elif obligation_type == ObligationType.LINEAGE_CAPTURE:
            requirement, error = _lineage_requirement(obligation, boundary_operator)
            if error is not None:
                diagnostics.append(error)
                continue
            assert requirement is not None
            instrumentation = _lineage_instrumentation(requirement, suffix)
            if instrumentation is None:
                diagnostics.append(
                    Diagnostic(
                        code=ReasonCode.LINEAGE_INSTRUMENTATION_MISSING,
                        message=(
                            "Validated plan lacks lineage instrumentation for a policy requirement."
                        ),
                        details={
                            "required_level": requirement.level.value,
                            "target_operator": requirement.target_operator,
                            "output_operator": output_operator,
                        },
                    )
                )
                continue
            lineage_requirements.append(requirement)
            lineage_instrumentation.append(instrumentation)
            # Logical validation proves that instrumentation was planned. It is
            # still pending until an execution certificate carries evidence.
            pending.append(obligation_type)
            continue
        else:
            enforced = _matches_suffix(obligation, suffix)

        if enforced:
            satisfied.append(obligation_type)
        else:
            diagnostics.append(
                _failure(
                    "Validated plan does not meet a policy obligation postcondition.",
                    obligation_type=obligation_type.value,
                    parameters=obligation.parameters,
                    boundary_operator=boundary_operator,
                    output_operator=output_operator,
                )
            )

    return ObligationVerification(
        tuple(satisfied),
        tuple(pending),
        tuple(lineage_requirements),
        tuple(lineage_instrumentation),
        tuple(diagnostics),
    )
