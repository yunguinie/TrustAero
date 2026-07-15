"""Generate a bounded set of approved DuckDB physical candidates."""

from __future__ import annotations

from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    PhysicalStrategySpec,
    ValidatedLogicalPlan,
)
from trustaero.planner.physical import plan_physical_execution


def generate_duckdb_candidates(
    plan: ValidatedLogicalPlan,
    *,
    materialization_targets: tuple[str, ...] = (),
    filter_orders: tuple[tuple[str, ...], ...] = (),
) -> tuple[ApprovedPhysicalPlan, ...]:
    """Generate fused, one-boundary, and bounded ordered-filter candidates.

    The input is already a ``ValidatedLogicalPlan``. A materialization boundary
    does not reorder, remove, or weaken any logical/governance operator; it only
    changes whether one validated intermediate relation is pipelined or stored.
    Duplicate targets are removed deterministically.
    """

    strategies = [PhysicalStrategySpec(strategy_id="fused")]
    for target in dict.fromkeys(materialization_targets):
        strategies.append(
            PhysicalStrategySpec(
                strategy_id=f"materialize-after-{target}",
                execution_mode="materialized",
                materialize_after=(target,),
            )
        )
    for order in dict.fromkeys(filter_orders):
        readable_order = "-then-".join(item.removeprefix("op-") for item in order)
        strategies.append(
            PhysicalStrategySpec(
                strategy_id=f"ordered-materialized-{readable_order}",
                execution_mode="ordered_materialized",
                filter_order=order,
            )
        )
    candidates = tuple(
        plan_physical_execution(plan, backend="duckdb", strategy=strategy)
        for strategy in strategies
    )
    blocked = tuple(
        dict.fromkeys(
            feature
            for candidate in candidates
            for feature in candidate.unimplemented_backend_features
        )
    )
    if blocked:
        raise ValueError(f"DuckDB candidate space contains unimplemented features: {blocked}")
    ids = [candidate.physical_plan_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Distinct physical strategies produced duplicate plan IDs")
    return candidates
