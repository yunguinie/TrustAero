"""Observe DuckDB physical plans instead of inferring them from SQL text."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, Self


class ExplainConnection(Protocol):
    """DuckDB methods needed by the physical-plan observer."""

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> Self: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


@dataclass(frozen=True)
class PhysicalPlanObservation:
    """Stable structure plus actual metrics from one DuckDB EXPLAIN result."""

    fingerprint: str
    operator_names: tuple[str, ...]
    actual_cardinalities: tuple[int, ...]
    rows_scanned: tuple[int, ...]
    operator_timings_ms: tuple[float, ...]
    max_intermediate_cardinality: int
    plan_json: str


def _stable_extra_info(extra_info: object) -> object:
    """Remove estimates that vary with statistics but keep semantic details."""

    if not isinstance(extra_info, dict):
        return extra_info
    return {
        key: value for key, value in sorted(extra_info.items()) if key != "Estimated Cardinality"
    }


def _operator_nodes(value: object) -> tuple[dict[str, Any], ...]:
    """Return physical operator nodes in pre-order from either JSON format."""

    nodes: list[dict[str, Any]] = []

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if "name" in node or "operator_name" in node:
            nodes.append(node)
        for child in node.get("children", []):
            visit(child)

    visit(value)
    return tuple(nodes)


def _structure(value: object) -> object:
    """Canonicalize only physical structure for cross-run fingerprinting."""

    if isinstance(value, list):
        return [_structure(item) for item in value]
    if not isinstance(value, dict):
        return value
    name = value.get("operator_name", value.get("name"))
    children = [_structure(child) for child in value.get("children", [])]
    if name is None:
        # EXPLAIN ANALYZE has a metric-only root wrapper. It is excluded from
        # the fingerprint so only the physical operator tree is compared.
        return children
    return {
        "name": name,
        "extra_info": _stable_extra_info(value.get("extra_info", {})),
        "children": children,
    }


def observe_duckdb_plan(
    connection: ExplainConnection,
    sql: str,
    parameters: tuple[Any, ...] = (),
    *,
    analyze: bool = True,
) -> PhysicalPlanObservation:
    """Run DuckDB EXPLAIN JSON and return a stable physical-plan observation.

    ``analyze=True`` executes the query and therefore captures real operator
    cardinalities and timings. Callers must use it only for read-only queries
    or deliberately isolated experimental setup statements.
    """

    prefix = "EXPLAIN (ANALYZE, FORMAT JSON)" if analyze else "EXPLAIN (FORMAT JSON)"
    row = connection.execute(f"{prefix} {sql}", parameters).fetchone()
    if row is None or len(row) < 2 or not isinstance(row[1], str):
        raise ValueError("DuckDB EXPLAIN did not return a JSON physical plan.")
    raw_plan = json.loads(row[1])
    structure = _structure(raw_plan)
    canonical = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    fingerprint = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    nodes = _operator_nodes(raw_plan)
    names = tuple(str(node.get("operator_name", node.get("name"))) for node in nodes)
    cardinalities = tuple(int(node.get("operator_cardinality", 0)) for node in nodes)
    rows_scanned = tuple(int(node.get("operator_rows_scanned", 0)) for node in nodes)
    timings_ms = tuple(float(node.get("operator_timing", 0.0)) * 1000.0 for node in nodes)
    return PhysicalPlanObservation(
        fingerprint=fingerprint,
        operator_names=names,
        actual_cardinalities=cardinalities,
        rows_scanned=rows_scanned,
        operator_timings_ms=timings_ms,
        max_intermediate_cardinality=max(cardinalities, default=0),
        plan_json=json.dumps(raw_plan, indent=2, sort_keys=True),
    )
