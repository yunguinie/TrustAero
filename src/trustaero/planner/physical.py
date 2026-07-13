"""Create the minimal approved physical-plan specification.

The current planner does not lower TrustAero IR to SQL or DuckDB operators. It
freezes a deterministic, auditable execution specification that a future
backend must implement before execution certificates can claim full verification.
"""

from __future__ import annotations

import hashlib
import json

from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    Operator,
    PhysicalOperatorSpec,
    ValidatedLogicalPlan,
)

_UNIMPLEMENTED_BY_OPERATOR = {
    "GeneralizeLocation": ("fixed_grid_coordinate_transform",),
    "Mask": ("mask_value_transform",),
    "MinGroupSize": ("minimum_group_suppression_guard",),
    "LineageCapture": ("lineage_backend_capture",),
}


def _physical_operator_id(logical_operator_id: str) -> str:
    return f"phys-{logical_operator_id}"


def _operator_spec(operator: Operator) -> PhysicalOperatorSpec:
    """Convert a validated logical operator into a backend-facing placeholder."""

    operator_type = operator.operator_type
    logical_operator_id = operator.operator_id
    unimplemented = _UNIMPLEMENTED_BY_OPERATOR.get(operator_type, ())
    return PhysicalOperatorSpec(
        physical_operator_id=_physical_operator_id(str(logical_operator_id)),
        logical_operator_id=str(logical_operator_id),
        operator_type=str(operator_type),
        inputs=tuple(_physical_operator_id(input_id) for input_id in operator.inputs),
        implementation_status="requires_backend" if unimplemented else "logical_only",
        unimplemented_features=unimplemented,
    )


def _physical_plan_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "pp-" + hashlib.sha256(encoded).hexdigest()[:16]


def plan_physical_execution(plan: ValidatedLogicalPlan) -> ApprovedPhysicalPlan:
    """Derive a deterministic pre-execution physical specification.

    The output is intentionally conservative: every operator remains
    ``not_bound`` to a concrete backend, and governance operators list the
    backend feature that still needs implementation.
    """

    physical_operators = tuple(_operator_spec(operator) for operator in plan.operators)
    unimplemented = tuple(
        dict.fromkeys(
            feature
            for operator in physical_operators
            for feature in operator.unimplemented_features
        )
    )
    payload: dict[str, object] = {
        "logical_plan_id": plan.logical_plan_id,
        "logical_plan_digest": plan.validation.canonical_digest,
        "operators": [operator.model_dump(mode="json") for operator in physical_operators],
        "output_operator": _physical_operator_id(plan.output_operator),
        "policy_snapshot": plan.bindings.policy_snapshot,
        "data_snapshots": plan.bindings.data_snapshots,
        "lineage_instrumentation": [
            item.model_dump(mode="json") for item in plan.lineage_instrumentation
        ],
        "pending_obligations": [item.value for item in plan.pending_obligations],
        "unimplemented_backend_features": list(unimplemented),
    }
    return ApprovedPhysicalPlan(
        physical_plan_id=_physical_plan_id(payload),
        logical_plan_id=plan.logical_plan_id,
        logical_plan_digest=plan.validation.canonical_digest,
        output_operator=_physical_operator_id(plan.output_operator),
        physical_operators=physical_operators,
        bindings=plan.bindings,
        lineage_instrumentation=plan.lineage_instrumentation,
        pending_obligations=plan.pending_obligations,
        unimplemented_backend_features=unimplemented,
        planner_notes=(
            "IR v1 physical planning is an auditable specification only; "
            "no SQL or DuckDB execution plan is emitted.",
        ),
    )
