"""Compile a small validated TrustAero IR fragment into parameterized SQL.

This module is intentionally *not* a general SQL generator. It is the first
trusted-executor boundary for experiments: only a narrow, independently tested
single-relation fragment is accepted. Unsupported operators fail closed instead
of being approximated with unsafe SQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from trustaero.catalog.models import Catalog
from trustaero.ir.enums import (
    AggregateFunction,
    ComparisonOperator,
    DataType,
    LineageLevel,
)
from trustaero.ir.models import (
    Aggregate,
    ApprovedPhysicalPlan,
    BooleanExpression,
    CandidatePlan,
    ComparisonExpression,
    Filter,
    GeneralizeLocation,
    Join,
    Limit,
    LineageCapture,
    LiteralExpression,
    Mask,
    NumericAffineExpression,
    NumericProductExpression,
    NumericProductFormulaExpression,
    Operator,
    Project,
    ScanSource,
    Sort,
    SpatialFilter,
    SpatialJoin,
    TemporalFilter,
    ValidatedLogicalPlan,
)
from trustaero.validator.type_checker import RelationSchema, type_check_plan


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
    physical_plan_id: str | None = None


_COMPARISON_SQL = {
    ComparisonOperator.EQ: "=",
    ComparisonOperator.NE: "<>",
    ComparisonOperator.GT: ">",
    ComparisonOperator.GE: ">=",
    ComparisonOperator.LT: "<",
    ComparisonOperator.LE: "<=",
}

_DUCKDB_TYPES = {
    DataType.STRING: "VARCHAR",
    DataType.INTEGER: "BIGINT",
    DataType.FLOAT: "DOUBLE",
    # Logical DECIMAL preserves exact base-10 comparison and arithmetic. Scale
    # is carried by real values; this wide form is used only for typed NULL.
    DataType.DECIMAL: "DECIMAL(38,18)",
    DataType.BOOLEAN: "BOOLEAN",
    # IR datetime literals always carry an offset, so DuckDB bindings use an
    # instant-preserving type instead of a session-timezone-dependent TIMESTAMP.
    DataType.DATETIME: "TIMESTAMPTZ",
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
    if expression.data_type == DataType.DECIMAL:
        return Decimal(str(expression.value))
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


def _compile_filter_expression(
    expression: ComparisonExpression | BooleanExpression,
) -> SqlFragment:
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


def _compile_sort(operator: Sort, input_fragment: SqlFragment) -> SqlFragment:
    """Compile validated sort keys without accepting free-form SQL text."""

    keys = ", ".join(
        f"{_quote_identifier(key.field)} {key.direction.upper()}" for key in operator.keys
    )
    return SqlFragment(
        f"SELECT * FROM ({input_fragment.sql}) AS input_rel ORDER BY {keys}",
        input_fragment.parameters,
    )


def _compile_limit(operator: Limit, input_fragment: SqlFragment) -> SqlFragment:
    """Compile a validated bounded Top-K suffix without free-form SQL."""

    return SqlFragment(
        f"SELECT * FROM ({input_fragment.sql}) AS input_rel LIMIT ?",
        input_fragment.parameters + (operator.row_count,),
    )


def _compile_filter(operator: Filter, input_fragment: SqlFragment) -> SqlFragment:
    predicate = _compile_filter_expression(operator.expression)
    return SqlFragment(
        f"SELECT * FROM ({input_fragment.sql}) AS input_rel WHERE {predicate.sql}",
        input_fragment.parameters + predicate.parameters,
    )


def _compile_join(
    operator: Join,
    left_fragment: SqlFragment,
    right_fragment: SqlFragment,
) -> SqlFragment:
    """Compile the V1 two-input equi-join fragment.

    Validation has already proved that key types match, keys are raw values,
    and output names do not collide. SQL NULL semantics are retained: NULL
    join keys do not match each other.
    """

    join_keyword = "INNER JOIN" if operator.join_type == "inner" else "LEFT JOIN"
    left_key = _quote_identifier(operator.left_field)
    right_key = _quote_identifier(operator.right_field)
    sql = (
        f"SELECT left_rel.*, right_rel.* FROM ({left_fragment.sql}) AS left_rel "
        f"{join_keyword} ({right_fragment.sql}) AS right_rel "
        f"ON left_rel.{left_key} = right_rel.{right_key}"
    )
    return SqlFragment(sql, left_fragment.parameters + right_fragment.parameters)


def _compile_spatial_join(
    operator: SpatialJoin,
    left_fragment: SqlFragment,
    right_fragment: SqlFragment,
) -> SqlFragment:
    """Compile the reviewed EPSG:4326 point-distance join using Haversine.

    The coordinate pairs were resolved against catalog spatial descriptors by
    the type checker.  Explicit fields are essential because the left relation
    may already carry several spatial descriptors after a previous join.
    ``least(1.0, ...)`` protects ``asin`` from floating-point overshoot.
    """

    if operator.relation != "distance_within" or operator.distance_km is None:
        raise ExecutionCompileError(
            "DuckDB SpatialJoin supports distance_within with distance_km only."
        )
    left_latitude, left_longitude = (_quote_identifier(field) for field in operator.left_fields)
    right_latitude, right_longitude = (_quote_identifier(field) for field in operator.right_fields)
    haversine_term = (
        f"power(sin(radians((right_rel.{right_latitude} - "
        f"left_rel.{left_latitude}) / 2.0)), 2) + "
        f"cos(radians(left_rel.{left_latitude})) * "
        f"cos(radians(right_rel.{right_latitude})) * "
        f"power(sin(radians((right_rel.{right_longitude} - "
        f"left_rel.{left_longitude}) / 2.0)), 2)"
    )
    distance = f"6371.0088 * 2.0 * asin(sqrt(least(1.0, {haversine_term})))"
    # Latitude difference is a sound necessary condition for a spherical
    # distance match. DuckDB can push this inexpensive bound before evaluating
    # the trigonometric predicate, avoiding a large cross product without
    # changing the accepted result set.
    latitude_bound = (
        f"abs(right_rel.{right_latitude} - left_rel.{left_latitude}) <= degrees(? / 6371.0088)"
    )
    sql = (
        f"SELECT left_rel.*, right_rel.* FROM ({left_fragment.sql}) AS left_rel "
        f"INNER JOIN ({right_fragment.sql}) AS right_rel "
        f"ON {latitude_bound} AND {distance} <= ?"
    )
    return SqlFragment(
        sql,
        left_fragment.parameters
        + right_fragment.parameters
        + (operator.distance_km, operator.distance_km),
    )


def _compile_aggregate(operator: Aggregate, input_fragment: SqlFragment) -> SqlFragment:
    """Compile grouped SQL aggregates with explicit output aliases."""

    select_items = [_quote_identifier(field) for field in operator.group_by]
    aggregate_parameters: list[Any] = []
    function_sql = {
        AggregateFunction.COUNT: "COUNT",
        AggregateFunction.SUM: "SUM",
        AggregateFunction.AVG: "AVG",
        AggregateFunction.MIN: "MIN",
        AggregateFunction.MAX: "MAX",
    }
    for expression in operator.aggregates:
        if expression.function == AggregateFunction.COUNT:
            argument = "*"
        elif isinstance(expression.input_expression, NumericProductExpression):
            argument = (
                f"({_quote_identifier(expression.input_expression.left.field)} * "
                f"{_quote_identifier(expression.input_expression.right.field)})"
            )
        elif isinstance(expression.input_expression, NumericProductFormulaExpression):
            compiled_factors: list[str] = []
            for factor in expression.input_expression.factors:
                if isinstance(factor, NumericAffineExpression):
                    arithmetic = "+" if factor.operator == "add" else "-"
                    compiled_factors.append(
                        f"(? {arithmetic} {_quote_identifier(factor.field.field)})"
                    )
                    aggregate_parameters.append(_literal_value(factor.constant))
                else:
                    compiled_factors.append(_quote_identifier(factor.field))
            argument = "(" + " * ".join(compiled_factors) + ")"
        else:
            argument = _quote_identifier(str(expression.input_field))
        select_items.append(
            f"{function_sql[expression.function]}({argument}) "
            f"AS {_quote_identifier(expression.output_field)}"
        )
    group_clause = ""
    if operator.group_by:
        group_clause = " GROUP BY " + ", ".join(
            _quote_identifier(field) for field in operator.group_by
        )
    return SqlFragment(
        f"SELECT {', '.join(select_items)} FROM ({input_fragment.sql}) AS input_rel{group_clause}",
        tuple(aggregate_parameters) + input_fragment.parameters,
    )


def _compile_mask(
    operator: Mask,
    input_fragment: SqlFragment,
    input_schema: RelationSchema,
) -> SqlFragment:
    """Compile the three deliberately incomparable Mask methods.

    ``redact`` emits a presentation token, ``hash`` uses DuckDB SHA-256 over a
    raw VARCHAR, and ``null`` emits a typed NULL. No method is treated as
    stronger than another, and later semantic reuse is rejected by validation.
    """

    replacements: list[str] = []
    parameters = list(input_fragment.parameters)
    for name in operator.fields:
        field = input_schema.get(name)
        if field is None:
            raise ExecutionCompileError(f"Mask input field is unavailable: {name}")
        quoted = _quote_identifier(name)
        if operator.method == "redact":
            replacements.append(f"CAST(? AS VARCHAR) AS {quoted}")
            parameters.append("[REDACTED]")
        elif operator.method == "hash":
            if field.data_type != DataType.STRING:
                raise ExecutionCompileError("Hash masking supports STRING fields only in IR v1.")
            replacements.append(f"sha256({quoted}) AS {quoted}")
        else:
            replacements.append(f"CAST(NULL AS {_DUCKDB_TYPES[field.data_type]}) AS {quoted}")
    return SqlFragment(
        f"SELECT * REPLACE ({', '.join(replacements)}) FROM ({input_fragment.sql}) AS input_rel",
        tuple(parameters),
    )


def _compile_generalize_location(
    operator: GeneralizeLocation,
    input_fragment: SqlFragment,
    input_schema: RelationSchema,
) -> SqlFragment:
    """Map one EPSG:4326 coordinate pair to deterministic grid-cell centers.

    V1 uses one angular step ``precision_km / 111.045`` for both axes, anchored
    at (-90, -180). This is an explicitly approximate geographic grid, not a
    distance calculation or a reinterpretation of earlier spatial selection.
    """

    targets = frozenset(operator.fields)
    matching = [descriptor for descriptor in input_schema.spatial if descriptor.fields == targets]
    if len(matching) != 1 or matching[0].crs != "EPSG:4326":
        raise ExecutionCompileError(
            "GeneralizeLocation requires one complete EPSG:4326 coordinate pair."
        )
    descriptor = matching[0]
    step = operator.precision_km / 111.045
    replacements: list[str] = []
    parameters = list(input_fragment.parameters)
    for field, offset in (
        (descriptor.latitude_field, 90.0),
        (descriptor.longitude_field, 180.0),
    ):
        quoted = _quote_identifier(field)
        replacements.append(
            f"floor(({quoted} + {offset}) / ?) * ? - {offset} + (? / 2.0) AS {quoted}"
        )
        parameters.extend((step, step, step))
    return SqlFragment(
        f"SELECT * REPLACE ({', '.join(replacements)}) FROM ({input_fragment.sql}) AS input_rel",
        tuple(parameters),
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
    schemas: Mapping[str, RelationSchema],
    materialize_after: str | None = None,
) -> SqlFragment:
    if operator_id in memo:
        return memo[operator_id]

    operator = operators[operator_id]
    if isinstance(operator, ScanSource):
        fragment = _compile_scan(operator, catalog, bindings)
    elif isinstance(operator, Project):
        fragment = _compile_project(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, Sort):
        fragment = _compile_sort(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, Limit):
        fragment = _compile_limit(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, Filter):
        fragment = _compile_filter(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, TemporalFilter):
        fragment = _compile_temporal_filter(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, SpatialFilter):
        scan = _single_scan_ancestor(operator.operator_id, operators, {})
        if scan is None:
            raise ExecutionCompileError("SpatialFilter execution requires one ScanSource ancestor.")
        fragment = _compile_spatial_filter(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
            scan,
            catalog,
        )
    elif isinstance(operator, Join):
        fragment = _compile_join(
            operator,
            _compile_operator(
                operator.inputs[0], operators, catalog, bindings, memo, schemas, materialize_after
            ),
            _compile_operator(
                operator.inputs[1], operators, catalog, bindings, memo, schemas, materialize_after
            ),
        )
    elif isinstance(operator, SpatialJoin):
        fragment = _compile_spatial_join(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
            _compile_operator(
                operator.inputs[1],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, Aggregate):
        fragment = _compile_aggregate(
            operator,
            _compile_operator(
                operator.inputs[0],
                operators,
                catalog,
                bindings,
                memo,
                schemas,
                materialize_after,
            ),
        )
    elif isinstance(operator, Mask):
        input_id = operator.inputs[0]
        fragment = _compile_mask(
            operator,
            _compile_operator(
                input_id, operators, catalog, bindings, memo, schemas, materialize_after
            ),
            schemas[input_id],
        )
    elif isinstance(operator, GeneralizeLocation):
        input_id = operator.inputs[0]
        fragment = _compile_generalize_location(
            operator,
            _compile_operator(
                input_id, operators, catalog, bindings, memo, schemas, materialize_after
            ),
            schemas[input_id],
        )
    elif isinstance(operator, LineageCapture):
        if operator.level != LineageLevel.SOURCE:
            raise ExecutionCompileError(
                "DuckDB V1 supports source-level lineage only; record lineage is unavailable."
            )
        # LineageCapture is a pass-through relational operator. Its evidence is
        # produced separately by the execution instrumentation module.
        fragment = _compile_operator(
            operator.inputs[0],
            operators,
            catalog,
            bindings,
            memo,
            schemas,
            materialize_after,
        )
    else:
        raise ExecutionCompileError(
            f"Operator {operator.operator_type} is not executable in the DuckDB V1 fragment."
        )

    if operator_id == materialize_after:
        # The target comes from an ApprovedPhysicalPlan, never from agent SQL.
        # MATERIALIZED is explicit so DuckDB cannot silently inline the
        # experimental storage boundary.
        fragment = SqlFragment(
            "WITH trustaero_boundary AS MATERIALIZED "
            f"({fragment.sql}) SELECT * FROM trustaero_boundary",
            fragment.parameters,
        )
    memo[operator_id] = fragment
    return fragment


def _execution_schemas(
    plan: ValidatedLogicalPlan,
    catalog: Catalog,
) -> Mapping[str, RelationSchema]:
    """Re-derive intermediate schemas at the execution trust boundary."""

    candidate = CandidatePlan(
        plan_id=plan.candidate_plan_id,
        request_context=plan.request_context,
        requested_output=plan.requested_output,
        operators=plan.operators,
        output_operator=plan.output_operator,
    )
    result = type_check_plan(candidate, catalog)
    if result.diagnostics:
        codes = ", ".join(diagnostic.code.value for diagnostic in result.diagnostics)
        raise ExecutionCompileError(f"Validated plan failed execution schema derivation: {codes}")
    return result.outputs


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
    schemas = _execution_schemas(plan, catalog)
    fragment = _compile_operator(
        plan.output_operator,
        operators,
        catalog,
        table_bindings,
        {},
        schemas,
    )
    return CompiledQuery(
        sql=fragment.sql,
        parameters=fragment.parameters,
        output_fields=tuple(field.name for field in plan.output_schema),
        logical_plan_id=plan.logical_plan_id,
        logical_plan_digest=plan.validation.canonical_digest,
    )


def compile_approved_physical_plan(
    plan: ValidatedLogicalPlan,
    physical_plan: ApprovedPhysicalPlan,
    catalog: Catalog,
    table_bindings: TableBindings,
) -> CompiledQuery:
    """Compile one approved DuckDB strategy fail-closed.

    This boundary independently checks that the physical object is bound to the
    exact validated logical plan and snapshots.  IR v1 realizes fused
    execution, one storage boundary, one independently re-derived safe Mask
    placement, or the same safe placement with a boundary immediately after
    that Mask. Unknown strategy modes remain fail-closed.
    """

    from trustaero.validator.physical_dag import validate_physical_plan_dag

    if physical_plan.logical_plan_id != plan.logical_plan_id:
        raise ExecutionCompileError("Physical plan is bound to a different logical plan.")
    if physical_plan.logical_plan_digest != plan.validation.canonical_digest:
        raise ExecutionCompileError("Physical plan logical digest does not match validation.")
    if physical_plan.bindings != plan.bindings:
        raise ExecutionCompileError("Physical plan snapshot bindings do not match validation.")
    if physical_plan.pending_obligations != plan.pending_obligations:
        raise ExecutionCompileError("Physical plan pending obligations do not match validation.")
    if physical_plan.unimplemented_backend_features:
        raise ExecutionCompileError("Physical plan contains unimplemented backend features.")
    dag_diagnostics = validate_physical_plan_dag(physical_plan)
    if dag_diagnostics:
        codes = ", ".join(item.code.value for item in dag_diagnostics)
        raise ExecutionCompileError(f"Approved physical plan DAG is invalid: {codes}")

    strategy = physical_plan.strategy
    materialize_after: str | None = None
    placement_modes = {"governance_placed", "governance_placed_materialized"}
    if strategy.execution_mode == "materialized":
        materialize_after = strategy.materialize_after[0]
        logical_ids = {operator.operator_id for operator in plan.operators}
        if materialize_after not in logical_ids or materialize_after == plan.output_operator:
            raise ExecutionCompileError("Physical materialization target is invalid.")
    elif strategy.execution_mode == "governance_placed_materialized":
        materialize_after = strategy.materialize_after[0]
        if materialize_after != strategy.placements[0].operator_id:
            raise ExecutionCompileError(
                "Placed materialization must target the moved governance operator."
            )
    elif strategy.execution_mode not in {"fused", "governance_placed"}:
        raise ExecutionCompileError(
            "Generic approved-plan compilation does not support this strategy mode."
        )

    execution_plan = plan
    if strategy.execution_mode in placement_modes:
        # Do not trust the candidate's rewired graph. Re-run the deterministic
        # planner from the validated logical plan and require an exact match.
        from trustaero.planner.physical import plan_physical_execution

        expected = plan_physical_execution(plan, backend="duckdb", strategy=strategy)
        if (
            physical_plan.physical_plan_id != expected.physical_plan_id
            or physical_plan.physical_operators != expected.physical_operators
            or physical_plan.output_operator != expected.output_operator
        ):
            raise ExecutionCompileError("Governance placement differs from trusted planning.")
        logical_by_id = {operator.operator_id: operator for operator in plan.operators}
        physical_by_id = {
            operator.physical_operator_id: operator for operator in physical_plan.physical_operators
        }
        rewired: list[Operator] = []
        for physical_operator in physical_plan.physical_operators:
            logical_operator = logical_by_id.get(physical_operator.logical_operator_id)
            if logical_operator is None:
                raise ExecutionCompileError("Physical plan references an unknown logical operator.")
            try:
                inputs = tuple(
                    physical_by_id[input_id].logical_operator_id
                    for input_id in physical_operator.inputs
                )
            except KeyError as exc:
                raise ExecutionCompileError(
                    "Physical plan contains an unbound placement input."
                ) from exc
            rewired.append(logical_operator.model_copy(update={"inputs": inputs}))
        output = physical_by_id.get(physical_plan.output_operator)
        if output is None:
            raise ExecutionCompileError("Physical placement output is unbound.")
        execution_plan = plan.model_copy(
            update={
                "operators": tuple(rewired),
                "output_operator": output.logical_operator_id,
            }
        )
    elif strategy.execution_mode != "fused" and strategy.execution_mode != "materialized":
        raise ExecutionCompileError(
            "Generic approved-plan compilation does not support this strategy mode."
        )

    operators = _operators_by_id(execution_plan)
    schemas = _execution_schemas(execution_plan, catalog)
    fragment = _compile_operator(
        execution_plan.output_operator,
        operators,
        catalog,
        table_bindings,
        {},
        schemas,
        materialize_after,
    )
    return CompiledQuery(
        sql=fragment.sql,
        parameters=fragment.parameters,
        output_fields=tuple(field.name for field in execution_plan.output_schema),
        logical_plan_id=plan.logical_plan_id,
        logical_plan_digest=plan.validation.canonical_digest,
        physical_plan_id=physical_plan.physical_plan_id,
    )
