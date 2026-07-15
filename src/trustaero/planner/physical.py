"""Create deterministic approved physical-plan specifications.

The default plan remains backend-neutral. Passing ``backend="duckdb"`` binds
only the explicitly implemented V1 fragment; unsupported features stay visible
instead of being silently treated as executable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from trustaero.ir.enums import LineageLevel
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    Operator,
    PhysicalOperatorSpec,
    PhysicalStrategySpec,
    ValidatedLogicalPlan,
)

_UNIMPLEMENTED_BY_OPERATOR = {
    "GeneralizeLocation": ("fixed_grid_coordinate_transform",),
    "Mask": ("mask_value_transform",),
    "MinGroupSize": ("minimum_group_suppression_guard",),
    "LineageCapture": ("lineage_backend_capture",),
}

_DUCKDB_EXECUTABLE = {
    "ScanSource",
    "Project",
    "Filter",
    "TemporalFilter",
    "SpatialFilter",
    "Join",
    "Aggregate",
    "Mask",
    "GeneralizeLocation",
}


def _physical_operator_id(logical_operator_id: str) -> str:
    return f"phys-{logical_operator_id}"


def _operator_spec(
    operator: Operator,
    backend: Literal["not_bound", "duckdb"],
) -> PhysicalOperatorSpec:
    """Convert a validated logical operator into a backend-facing placeholder."""

    operator_type = operator.operator_type
    logical_operator_id = operator.operator_id
    status: Literal["logical_only", "executable", "requires_backend"]
    if backend == "not_bound":
        unimplemented = _UNIMPLEMENTED_BY_OPERATOR.get(operator_type, ())
        status = "requires_backend" if unimplemented else "logical_only"
    elif operator_type == "LineageCapture":
        # Source lineage has a concrete implementation. Record provenance is
        # still rejected until row-level identities are carried by execution.
        level = getattr(operator, "level", None)
        unimplemented = () if level == LineageLevel.SOURCE else ("record_lineage_capture",)
        status = "executable" if not unimplemented else "requires_backend"
    elif operator_type in _DUCKDB_EXECUTABLE:
        unimplemented = ()
        status = "executable"
    else:
        unimplemented = (f"duckdb_{str(operator_type).lower()}_execution",)
        status = "requires_backend"
    return PhysicalOperatorSpec(
        physical_operator_id=_physical_operator_id(str(logical_operator_id)),
        logical_operator_id=str(logical_operator_id),
        operator_type=str(operator_type),
        inputs=tuple(_physical_operator_id(input_id) for input_id in operator.inputs),
        backend=backend,
        implementation_status=status,
        unimplemented_features=unimplemented,
    )


def _physical_plan_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "pp-" + hashlib.sha256(encoded).hexdigest()[:16]


def _ordered_filter_specs(
    plan: ValidatedLogicalPlan,
    physical_operators: tuple[PhysicalOperatorSpec, ...],
    filter_order: tuple[str, ...],
) -> tuple[PhysicalOperatorSpec, ...]:
    """Rewire one complete, linear pure-filter chain in an approved order.

    This is intentionally narrower than general operator reordering. Spatial,
    temporal, and comparison filters in IR v1 are total, side-effect-free row
    selectors, so conjunction is commutative. Branches, partial chains, masks,
    joins, aggregates, and unknown operators fail closed.
    """

    by_id = {operator.operator_id: operator for operator in plan.operators}
    selected = set(filter_order)
    if any(operator_id not in by_id for operator_id in filter_order):
        raise ValueError("Ordered filter strategy refers outside the logical plan")
    allowed_types = {"Filter", "SpatialFilter", "TemporalFilter"}
    selected_operators = [by_id[operator_id] for operator_id in filter_order]
    if any(operator.operator_type not in allowed_types for operator in selected_operators):
        raise ValueError("Ordered strategy supports only the pure IR v1 filter fragment")
    if any(len(operator.inputs) != 1 for operator in selected_operators):
        raise ValueError("Ordered filters must form a unary chain")

    external_inputs = [
        (operator.operator_id, operator.inputs[0])
        for operator in selected_operators
        if operator.inputs[0] not in selected
    ]
    internal_edge_count = sum(operator.inputs[0] in selected for operator in selected_operators)
    external_consumers = [
        (operator.operator_id, input_id)
        for operator in plan.operators
        if operator.operator_id not in selected
        for input_id in operator.inputs
        if input_id in selected
    ]
    internal_consumer_counts = {
        operator_id: sum(
            input_id == operator_id
            for operator in selected_operators
            for input_id in operator.inputs
        )
        for operator_id in selected
    }
    if (
        len(external_inputs) != 1
        or internal_edge_count != len(selected) - 1
        or len(external_consumers) != 1
        or sorted(internal_consumer_counts.values()) != [0] + [1] * (len(selected) - 1)
    ):
        raise ValueError("Ordered filters must cover one complete, unbranched logical chain")

    root_input = external_inputs[0][1]
    external_consumer_id = external_consumers[0][0]
    upstream_is_filter = root_input in by_id and by_id[root_input].operator_type in allowed_types
    downstream_is_filter = by_id[external_consumer_id].operator_type in allowed_types
    if upstream_is_filter or downstream_is_filter:
        raise ValueError("Ordered filters must cover the maximal adjacent filter chain")

    original_tail = external_consumers[0][1]
    new_tail = filter_order[-1]
    ordered_inputs = {
        operator_id: root_input if index == 0 else filter_order[index - 1]
        for index, operator_id in enumerate(filter_order)
    }
    original_tail_physical = _physical_operator_id(original_tail)
    new_tail_physical = _physical_operator_id(new_tail)
    rewired: list[PhysicalOperatorSpec] = []
    for operator in physical_operators:
        inputs: tuple[str, ...]
        if operator.logical_operator_id in selected:
            inputs = (_physical_operator_id(ordered_inputs[operator.logical_operator_id]),)
        else:
            inputs = tuple(
                new_tail_physical if item == original_tail_physical else item
                for item in operator.inputs
            )
        rewired.append(operator.model_copy(update={"inputs": inputs}))
    return tuple(rewired)


def plan_physical_execution(
    plan: ValidatedLogicalPlan,
    *,
    backend: Literal["not_bound", "duckdb"] = "not_bound",
    strategy: PhysicalStrategySpec | None = None,
) -> ApprovedPhysicalPlan:
    """Derive a deterministic pre-execution physical specification.

    The DuckDB path is allow-listed. Adding a new IR operator therefore cannot
    make it executable merely because its name resembles a SQL operator.
    """

    selected_strategy = strategy or PhysicalStrategySpec(strategy_id="fused")
    logical_ids = {operator.operator_id for operator in plan.operators}
    if selected_strategy.execution_mode == "materialized":
        if backend != "duckdb":
            raise ValueError("IR v1 materialization is implemented for DuckDB only")
        target = selected_strategy.materialize_after[0]
        if target not in logical_ids:
            raise ValueError(f"Materialization target is not in the logical plan: {target}")
        if target == plan.output_operator:
            raise ValueError("Materializing after the final output is not a useful IR v1 candidate")

    if selected_strategy.execution_mode == "ordered_materialized" and backend != "duckdb":
        raise ValueError("Ordered filter execution is implemented for DuckDB only")

    physical_operators = tuple(_operator_spec(operator, backend) for operator in plan.operators)
    if selected_strategy.execution_mode == "ordered_materialized":
        physical_operators = _ordered_filter_specs(
            plan,
            physical_operators,
            selected_strategy.filter_order,
        )
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
        "backend": backend,
        "strategy": selected_strategy.model_dump(mode="json"),
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
        strategy=selected_strategy,
        bindings=plan.bindings,
        lineage_instrumentation=plan.lineage_instrumentation,
        pending_obligations=plan.pending_obligations,
        unimplemented_backend_features=unimplemented,
        planner_notes=(
            (
                "IR v1 physical planning is an auditable specification only; "
                "no concrete backend is bound."
                if backend == "not_bound"
                else (
                    "DuckDB is bound only for the allow-listed executable IR v1 fragment; "
                    f"physical strategy={selected_strategy.strategy_id}."
                )
            ),
        ),
    )
