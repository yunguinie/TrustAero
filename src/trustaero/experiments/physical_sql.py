"""Strict SQL realization of the bounded Phase 2 DuckDB strategy fragment.

The compiler accepts only physical decisions whose semantics have been frozen
here.  A materialization boundary changes pipelining, but never removes,
reorders, or weakens a logical predicate.  Unknown boundaries fail closed.
"""

from __future__ import annotations

from trustaero.ir.models import ApprovedPhysicalPlan

_SUPPORTED_BOUNDARIES = frozenset(
    {
        "op-temporal",
        "op-spatial",
        "op-policy",
        "op-event-project",
    }
)


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


def compile_phase2_strategy(candidate: ApprovedPhysicalPlan) -> str:
    """Compile one approved Phase 2 strategy to result-equivalent DuckDB SQL.

    This is deliberately not a general SQL optimizer.  It realizes only the
    four independently reviewable materialization positions in the controlled
    experiment query.  The caller still has to compare returned rows and
    inspect DuckDB's actual physical plan.
    """

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
