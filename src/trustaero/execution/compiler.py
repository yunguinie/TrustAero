"""Compile a small validated TrustAero IR fragment into parameterized SQL.

This module is intentionally *not* a general SQL generator. It is the first
trusted-executor boundary for experiments: only a narrow, independently tested
single-relation fragment is accepted. Unsupported operators fail closed instead
of being approximated with unsafe SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trustaero.catalog.models import Catalog
from trustaero.ir.enums import ComparisonOperator, DataType
from trustaero.ir.models import (
    BooleanExpression,
    ComparisonExpression,
    Filter,
    LiteralExpression,
    Operator,
    Project,
    ScanSource,
    SpatialFilter,
    TemporalFilter,
    ValidatedLogicalPlan,
)


class ExecutionCompileError(ValueError):
    """Raised when a validated plan is outside the executable V1 fragment."""


@dataclass(frozen=True)
class TableBindings:
    """Map catalog dataset IDs to trusted physical table names.

    Agent-provided plans never get to choose SQL table names directly. The
    caller binds each governed dataset to a trusted table or view name.
    """

    dataset_tables: dict[str, str]


@dataclass(frozen=True)
class SqlFragment:
    """Internal SQL fragment plus positional parameters."""

    sql: str
    parameters: tuple[Any, ...] = ()


@dataclass(frozen=True)
class CompiledQuery:
    """A parameterized SQL query ready for a trusted DB-API style executor."""

    sql: str
    parameters: tuple[Any, ...]
    output_fields: tuple[str, ...]
    logical_plan_id: str
    logical_plan_digest: str


_COMPARISON_SQL = {
    ComparisonOperator.EQ: "=",
    ComparisonOperator.NE: "<>",
    ComparisonOperator.GT: ">",
    ComparisonOperator.GE: ">=",
    ComparisonOperator.LT: "<",
    ComparisonOperator.LE: "<=",
}


def _quote_identifier(identifier: str) -> str:
    """Quote a trusted identifier for DuckDB-compatible SQL."""

    if not identifier:
        raise ExecutionCompileError("SQL identifier cannot be empty.")
    return '"' + identifier.replace('"', '""') + '"'


def _literal_value(expression: LiteralExpression) -> Any:
    """Convert typed IR literals to DB-API parameters.

    Values remain parameters instead of being interpolated into SQL. This keeps
    the executor boundary independent from whatever text an untrusted agent put
    in the original plan.
    """

    if expression.data_type == DataType.DATETIME:
        return datetime.fromisoformat(str(expression.value).replace("Z", "+00:00"))
    return expression.value


def _compile_comparison(expression: ComparisonExpression) -> SqlFragment:
    """Compile the deliberately small field-vs-literal comparison fragment."""

    left = _quote_identifier(expression.left.field)
    operator = _COMPARISON_SQL[expression.operator]
    predicate = f"{left} {operator} ?"
    if expression.negated:
        predicate = f"NOT ({predicate})"
    return SqlFragment(predicate, (_literal_value(expression.right),))


def _compile_boolean(expression: BooleanExpression) -> SqlFragment:
    """Compile a flat AND/OR over comparison predicates."""

    fragments = tuple(_compile_comparison(item) for item in expression.operands)
    joiner = f" {expression.operator.upper()} "
    sql = joiner.join(f"({fragment.sql})" for fragment in fragments)
    if expression.negated:
        sql = f"NOT ({sql})"
    parameters = tuple(value for fragment in fragments for value in fragment.parameters)
    return SqlFragment(sql, parameters)


def _compile_filter_expression(expression: ComparisonExpression | BooleanExpression) -> SqlFragment:
    if isinstance(expression, ComparisonExpression):
        return _compile_comparison(expression)
    return _compile_boolean(expression)


def _compile_scan(operator: ScanSource, catalog: Catalog, bindings: TableBindings) -> SqlFragment:
    dataset = catalog.get_dataset(operator.dataset)
    if dataset is None:
        # A validated plan should already have resolved this, but execution is
        # another trust boundary and must still fail closed on catalog drift.
        raise ExecutionCompileError(f"Unknown dataset at execution time: {operator.dataset}")
    table = bindings.dataset_tables.get(operator.dataset)
    if table is None:
        raise ExecutionCompileError(f"No trusted table binding for dataset: {operator.dataset}")
    return SqlFragment(f"SELECT * FROM {_quote_identifier(table)}")


def _compile_project(operator: Project, input_fragment: SqlFragment) -> SqlFragment:
    fields = ", ".join(_quote_identifier(field) for field in operator.fields)
    return SqlFragment(
        f"SELECT {fields} FROM ({input_fragment.sql}) AS input_rel",
        input_fragment.parameters,
    )


def _compile_filter(operator: Filter, input_fragment: SqlFragment) -> SqlFragment:
    predicate = _compile_filter_expression(operator.expression)
    return SqlFragment(
        f"SELECT * FROM ({input_fragment.sql}) AS input_rel WHERE {predicate.sql}",
        input_fragment.parameters + predicate.parameters,
    )


def _compile_temporal_filter(operator: TemporalFilter, input_fragment: SqlFragment) -> SqlFragment:
    field = _quote_identifier(operator.field)
    return SqlFragment(
        f"SELECT * FROM ({input_fragment.sql}) AS input_rel WHERE {field} >= ? AND {field} < ?",
        input_fragment.parameters + (operator.start, operator.end),
    )


def _compile_spatial_filter(
    operator: SpatialFilter,
    input_fragment: SqlFragment,
    scan_operator: ScanSource,
    catalog: Catalog,
) -> SqlFragment:
    """Compile a conservative approximate-radius predicate for smoke tests.

    This is not a geodesic engine. It is a deterministic small-fragment
    approximation for early DuckDB experiments over latitude/longitude columns.
    Full spatial semantics belong in a later backend-specific implementation.
    """

    dataset = catalog.get_dataset(scan_operator.dataset)
    if dataset is None or dataset.spatial is None:
        raise ExecutionCompileError("SpatialFilter requires catalog spatial metadata.")
    lat_field = _quote_identifier(dataset.spatial.latitude_field)
    lon_field = _quote_identifier(dataset.spatial.longitude_field)
    center_lat, center_lon = operator.center
    distance_expr = (
        f"111.045 * sqrt(power({lat_field} - ?, 2) + power(({lon_field} - ?) * cos(radians(?)), 2))"
    )
    return SqlFragment(
        f"SELECT * FROM ({input_fragment.sql}) AS input_rel WHERE {distance_expr} <= ?",
        input_fragment.parameters + (center_lat, center_lon, center_lat, operator.radius_km),
    )


def _operators_by_id(plan: ValidatedLogicalPlan) -> dict[str, Operator]:
    return {operator.operator_id: operator for operator in plan.operators}


def _single_scan_ancestor(
    operator_id: str,
    operators: dict[str, Operator],
    memo: dict[str, ScanSource | None],
) -> ScanSource | None:
    """Return the unique ScanSource ancestor for the supported single-table fragment."""

    if operator_id in memo:
        return memo[operator_id]
    operator = operators[operator_id]
    if isinstance(operator, ScanSource):
        memo[operator_id] = operator
        return operator
    scans = tuple(
        scan
        for input_id in operator.inputs
        if (scan := _single_scan_ancestor(input_id, operators, memo)) is not None
    )
    unique = {scan.operator_id: scan for scan in scans}
    memo[operator_id] = next(iter(unique.values())) if len(unique) == 1 else None
    return memo[operator_id]


def _compile_operator(
    operator_id: str,
    operators: dict[str, Operator],
    catalog: Catalog,
    bindings: TableBindings,
    memo: dict[str, SqlFragment],
) -> SqlFragment:
    if operator_id in memo:
        return memo[operator_id]

    operator = operators[operator_id]
    if isinstance(operator, ScanSource):
        fragment = _compile_scan(operator, catalog, bindings)
    elif isinstance(operator, Project):
        fragment = _compile_project(
            operator,
            _compile_operator(operator.inputs[0], operators, catalog, bindings, memo),
        )
    elif isinstance(operator, Filter):
        fragment = _compile_filter(
            operator,
            _compile_operator(operator.inputs[0], operators, catalog, bindings, memo),
        )
    elif isinstance(operator, TemporalFilter):
        fragment = _compile_temporal_filter(
            operator,
            _compile_operator(operator.inputs[0], operators, catalog, bindings, memo),
        )
    elif isinstance(operator, SpatialFilter):
        scan = _single_scan_ancestor(operator.operator_id, operators, {})
        if scan is None:
            raise ExecutionCompileError("SpatialFilter execution requires one ScanSource ancestor.")
        fragment = _compile_spatial_filter(
            operator,
            _compile_operator(operator.inputs[0], operators, catalog, bindings, memo),
            scan,
            catalog,
        )
    else:
        raise ExecutionCompileError(
            f"Operator {operator.operator_type} is not executable in the DuckDB V1 fragment."
        )

    memo[operator_id] = fragment
    return fragment


def compile_validated_plan(
    plan: ValidatedLogicalPlan,
    catalog: Catalog,
    table_bindings: TableBindings,
) -> CompiledQuery:
    """Compile a validated logical plan into one parameterized SQL statement.

    The input must already be a ``ValidatedLogicalPlan``. This function does not
    re-authorize the plan; it only refuses unsupported backend fragments and
    binds governed dataset IDs to caller-provided trusted table names.
    """

    operators = _operators_by_id(plan)
    if plan.output_operator not in operators:
        raise ExecutionCompileError("Validated plan output operator is missing.")
    fragment = _compile_operator(plan.output_operator, operators, catalog, table_bindings, {})
    return CompiledQuery(
        sql=fragment.sql,
        parameters=fragment.parameters,
        output_fields=tuple(field.name for field in plan.output_schema),
        logical_plan_id=plan.logical_plan_id,
        logical_plan_digest=plan.validation.canonical_digest,
    )
