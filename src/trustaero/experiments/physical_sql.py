"""Strict SQL realization of the bounded Phase 2 DuckDB strategy fragment.

The compiler accepts only physical decisions whose semantics have been frozen
here. Ordinary boundaries preserve logical order. The ordered fragment is a
complete permutation of three pure filters and materializes every stage, so
DuckDB cannot silently flatten the experimental order. Unknown choices fail
closed.
"""

from __future__ import annotations

from trustaero.ir.models import ApprovedPhysicalPlan, Mask, ValidatedLogicalPlan

_SUPPORTED_BOUNDARIES = frozenset(
    {
        "op-temporal",
        "op-spatial",
        "op-policy",
        "op-event-project",
    }
)
_ORDERED_FILTERS = frozenset({"op-temporal", "op-spatial", "op-policy"})


def _temporal(alias: str) -> str:
    return (
        f"{alias}.event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'\n"
        f"AND {alias}.event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'"
    )


def _spatial(alias: str) -> str:
    return (
        "111.045 * sqrt(\n"
        f"  power({alias}.latitude - 40.0, 2)\n"
        f"  + power(({alias}.longitude - 116.3) * cos(radians(40.0)), 2)\n"
        ") <= 20.0"
    )


def _policy(alias: str) -> str:
    return f"{alias}.policy_allowed"


def _filter_predicate(operator_id: str, alias: str) -> str:
    functions = {
        "op-temporal": _temporal,
        "op-spatial": _spatial,
        "op-policy": _policy,
    }
    try:
        return functions[operator_id](alias)
    except KeyError as exc:
        raise ValueError(f"Unsupported ordered Phase 2 filter: {operator_id}") from exc


def _where(*predicates: str) -> str:
    return "\nAND ".join(predicates)


def _join_sql(
    source_relation: str,
    source_alias: str,
    *,
    remaining_predicates: tuple[str, ...] = (),
) -> str:
    where_clause = f"\nWHERE {_where(*remaining_predicates)}" if remaining_predicates else ""
    return (
        f"SELECT {source_alias}.event_id, dimension.severity_label\n"
        f"FROM {source_relation} AS {source_alias}\n"
        "INNER JOIN severity_dim AS dimension\n"
        f"  ON {source_alias}.join_key = dimension.dimension_key"
        f"{where_clause}\n"
        f"ORDER BY {source_alias}.event_id"
    )


def _compile_masked_phase2_strategy(
    candidate: ApprovedPhysicalPlan,
    logical_plan: ValidatedLogicalPlan,
) -> str:
    """Compile the reviewed event-id hash placement experiment."""

    masks = [operator for operator in logical_plan.operators if isinstance(operator, Mask)]
    if len(masks) != 1 or masks[0].fields != ("event_id",) or masks[0].method != "hash":
        raise ValueError("Phase 2 Mask placement supports only hash(event_id)")
    mask = masks[0]
    predicates = (_temporal("events"), _spatial("events"), _policy("events"))
    strategy = candidate.strategy
    if strategy.execution_mode == "fused":
        return (
            "SELECT sha256(joined.event_id) AS event_id, joined.severity_label\n"
            "FROM (\n"
            "  SELECT events.event_id, dimension.severity_label\n"
            "  FROM synthetic_events AS events\n"
            "  INNER JOIN severity_dim AS dimension\n"
            "    ON events.join_key = dimension.dimension_key\n"
            f"  WHERE {_where(*predicates)}\n"
            ") AS joined\n"
            "ORDER BY event_id"
        )
    if strategy.execution_mode != "governance_placed":
        raise ValueError("Mask experiment accepts only fused or placed execution")
    placement = strategy.placements[0]
    if (
        placement.operator_id != mask.operator_id
        or placement.after_operator_id != "op-event-project"
    ):
        raise ValueError("Unsupported Phase 2 Mask placement")
    return (
        "WITH masked_events AS MATERIALIZED (\n"
        "  SELECT sha256(events.event_id) AS event_id, events.join_key\n"
        "  FROM synthetic_events AS events\n"
        f"  WHERE {_where(*predicates)}\n"
        ")\n"
        "SELECT masked_events.event_id, dimension.severity_label\n"
        "FROM masked_events\n"
        "INNER JOIN severity_dim AS dimension\n"
        "  ON masked_events.join_key = dimension.dimension_key\n"
        "ORDER BY masked_events.event_id"
    )


def compile_phase2_strategy(
    candidate: ApprovedPhysicalPlan,
    logical_plan: ValidatedLogicalPlan | None = None,
) -> str:
    """Compile one approved Phase 2 strategy to result-equivalent DuckDB SQL.

    This is deliberately not a general SQL optimizer. It realizes only the
    independently reviewable materialization decisions in the controlled
    experiment query. The caller still has to compare returned rows and
    inspect DuckDB's actual physical plan.
    """

    has_mask = any(operator.operator_type == "Mask" for operator in candidate.physical_operators)
    if has_mask:
        if logical_plan is None:
            raise ValueError("Mask compilation requires the bound validated logical plan")
        return _compile_masked_phase2_strategy(candidate, logical_plan)

    strategy = candidate.strategy
    if strategy.execution_mode == "fused":
        return _join_sql(
            "synthetic_events",
            "events",
            remaining_predicates=(
                _temporal("events"),
                _spatial("events"),
                _policy("events"),
            ),
        )

    if strategy.execution_mode == "ordered_materialized":
        order = strategy.filter_order
        if len(order) != 3 or set(order) != _ORDERED_FILTERS:
            raise ValueError("Ordered Phase 2 strategy must permute all three filters")
        ctes: list[str] = []
        source_relation = "synthetic_events"
        source_alias = "events"
        for index, operator_id in enumerate(order, start=1):
            stage = f"ordered_stage_{index}"
            ctes.append(
                f"{stage} AS MATERIALIZED (\n"
                "  SELECT *\n"
                f"  FROM {source_relation} AS {source_alias}\n"
                f"  WHERE {_filter_predicate(operator_id, source_alias)}\n"
                ")"
            )
            source_relation = stage
            source_alias = f"stage_{index}"
        return "WITH\n" + ",\n".join(ctes) + "\n" + _join_sql(source_relation, source_alias)

    boundary = strategy.materialize_after[0]
    if boundary not in _SUPPORTED_BOUNDARIES:
        raise ValueError(f"Unsupported Phase 2 materialization boundary: {boundary}")

    predicates = {
        "op-temporal": (_temporal("events"),),
        "op-spatial": (_temporal("events"), _spatial("events")),
        "op-policy": (
            _temporal("events"),
            _spatial("events"),
            _policy("events"),
        ),
        "op-event-project": (
            _temporal("events"),
            _spatial("events"),
            _policy("events"),
        ),
    }[boundary]
    selected_fields = (
        "events.event_id, events.join_key" if boundary == "op-event-project" else "events.*"
    )
    remaining = {
        "op-temporal": (_spatial("stage"), _policy("stage")),
        "op-spatial": (_policy("stage"),),
        "op-policy": (),
        "op-event-project": (),
    }[boundary]
    return (
        "WITH stage AS MATERIALIZED (\n"
        f"  SELECT {selected_fields}\n"
        "  FROM synthetic_events AS events\n"
        f"  WHERE {_where(*predicates)}\n"
        ")\n" + _join_sql("stage", "stage", remaining_predicates=remaining)
    )


def supported_phase2_materialization_targets() -> tuple[str, ...]:
    """Return the deterministic, reviewed boundary order used by Phase 2B."""

    return ("op-temporal", "op-spatial", "op-policy", "op-event-project")


def supported_phase2_filter_orders() -> tuple[tuple[str, ...], ...]:
    """Return every reviewed permutation of the complete pure-filter chain."""

    return (
        ("op-temporal", "op-spatial", "op-policy"),
        ("op-temporal", "op-policy", "op-spatial"),
        ("op-spatial", "op-temporal", "op-policy"),
        ("op-spatial", "op-policy", "op-temporal"),
        ("op-policy", "op-temporal", "op-spatial"),
        ("op-policy", "op-spatial", "op-temporal"),
    )
