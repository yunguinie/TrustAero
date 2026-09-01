"""Generate a bounded set of approved DuckDB physical candidates."""

from __future__ import annotations

from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    PhysicalOperatorPlacementSpec,
    PhysicalStrategySpec,
    ValidatedLogicalPlan,
)
from trustaero.planner.physical import plan_physical_execution


def generate_duckdb_candidates(
    plan: ValidatedLogicalPlan,
    *,
    materialization_targets: tuple[str, ...] = (),
    filter_orders: tuple[tuple[str, ...], ...] = (),
    operator_placements: tuple[tuple[str, str], ...] = (),
    materialized_operator_placements: tuple[tuple[str, str], ...] = (),
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
    for operator_id, after_operator_id in dict.fromkeys(operator_placements):
        strategies.append(
            PhysicalStrategySpec(
                strategy_id=(
                    f"place-{operator_id.removeprefix('gov-')}-after-"
                    f"{after_operator_id.removeprefix('op-')}"
                ),
                execution_mode="governance_placed",
                placements=(
                    PhysicalOperatorPlacementSpec(
                        operator_id=operator_id,
                        after_operator_id=after_operator_id,
                    ),
                ),
            )
        )
    for operator_id, after_operator_id in dict.fromkeys(materialized_operator_placements):
        # This is not arbitrary "placement plus boundary" composition.  The
        # strategy model requires the sole boundary to be the moved Mask, and
        # the physical planner independently proves that moving it is safe.
        strategies.append(
            PhysicalStrategySpec(
                strategy_id=(
                    f"materialize-place-{operator_id.removeprefix('gov-')}-after-"
                    f"{after_operator_id.removeprefix('op-')}"
                ),
                execution_mode="governance_placed_materialized",
                materialize_after=(operator_id,),
                placements=(
                    PhysicalOperatorPlacementSpec(
                        operator_id=operator_id,
                        after_operator_id=after_operator_id,
                    ),
                ),
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
