"""Tests for the minimal trusted execution compiler."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
from typing import Any

import pytest

from trustaero.execution import (
    CompiledQuery,
    DuckDBUnavailable,
    ExecutionCompileError,
    TableBindings,
    compile_approved_physical_plan,
    compile_validated_plan,
    execute_with_connection,
    execute_with_duckdb,
)
from trustaero.ir.enums import DataType, ReasonCode, ValidationStatus
from trustaero.ir.models import LiteralExpression
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate


def _validated_plan(raw_plan: dict[str, Any], policy_set: object, catalog: object):
    response = validate(raw_plan, policy_set, catalog)  # type: ignore[arg-type]
    assert response.status in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}
    assert response.validated_plan is not None
    return response.validated_plan


def test_compile_accept_plan_to_parameterized_sql(accept_plan, policy_set, catalog) -> None:
    """A validated single-table projection can be lowered to a safe SQL fragment."""

    plan = _validated_plan(accept_plan, policy_set, catalog)

    compiled = compile_validated_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
    )

    assert compiled.output_fields == ("event_id", "magnitude")
    assert compiled.parameters == ()
    assert compiled.sql == (
        'SELECT "event_id", "magnitude" FROM (SELECT * FROM "earthquake_events") AS input_rel'
    )


def test_compile_filter_uses_parameters_not_literal_interpolation(
    accept_plan, policy_set, catalog
) -> None:
    """Untrusted literal text must become a DB parameter, not part of SQL text."""

    raw = copy.deepcopy(accept_plan)
    raw["operators"].insert(
        1,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter",
            "inputs": ["op1"],
            "expression": {
                "expression_type": "comparison",
                "operator": "ge",
                "left": {"expression_type": "field", "field": "magnitude"},
                "right": {
                    "expression_type": "literal",
                    "data_type": "float",
                    "value": 4.5,
                },
            },
        },
    )
    raw["operators"][2]["inputs"] = ["op-filter"]
    plan = _validated_plan(raw, policy_set, catalog)

    compiled = compile_validated_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
    )

    assert "magnitude" in compiled.sql
    assert "4.5" not in compiled.sql
    assert compiled.parameters == (4.5,)


def test_compile_rejects_record_lineage_until_backend_semantics_exist(
    rewrite_plan, policy_set, catalog
) -> None:
    """Implemented generalization must not make record lineage executable."""

    plan = _validated_plan(rewrite_plan, policy_set, catalog)

    with pytest.raises(ExecutionCompileError, match="record lineage"):
        compile_validated_plan(
            plan,
            catalog,
            TableBindings(dataset_tables={"critical_facilities": "facilities"}),
        )


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConnection:
    def __init__(self) -> None:
        self.seen_sql: str | None = None
        self.seen_parameters: tuple[object, ...] | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _FakeCursor:
        self.seen_sql = query
        self.seen_parameters = parameters
        return _FakeCursor([("eq-1", 5.2), ("eq-2", 4.8)])


def test_execute_with_connection_hashes_materialized_result(
    accept_plan, policy_set, catalog
) -> None:
    """The executor returns a stable result digest for certificate binding."""

    plan = _validated_plan(accept_plan, policy_set, catalog)
    compiled = compile_validated_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
    )
    connection = _FakeConnection()

    result = execute_with_connection(compiled, connection)

    assert connection.seen_sql == compiled.sql
    assert connection.seen_parameters == ()
    assert result.row_count == 2
    assert result.columns == ("event_id", "magnitude")
    assert result.result_digest.startswith("sha256:")


def test_execute_with_duckdb_reports_missing_optional_dependency(
    accept_plan, policy_set, catalog
) -> None:
    """Without the optional package, users get an actionable TrustAero error."""

    if importlib.util.find_spec("duckdb") is not None:
        pytest.skip("DuckDB is installed; missing-dependency path is not active.")

    plan = _validated_plan(accept_plan, policy_set, catalog)
    compiled = compile_validated_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
    )

    with pytest.raises(DuckDBUnavailable):
        execute_with_duckdb(compiled)


def test_datetime_literal_expression_remains_validated_by_ir_model() -> None:
    """Keep a tiny regression check for datetime parameter conversion inputs."""

    literal = LiteralExpression(
        expression_type="literal",
        data_type=DataType.DATETIME,
        value="2026-01-01T00:00:00+00:00",
    )

    assert literal.value == "2026-01-01T00:00:00+00:00"


def _execute_real_duckdb(
    raw_plan: dict[str, Any],
    policy_set: object,
    catalog: object,
    setup_sql: tuple[str, ...],
):
    """Validate, compile, and execute one small plan against in-memory DuckDB."""

    duckdb = pytest.importorskip("duckdb")
    plan = _validated_plan(raw_plan, policy_set, catalog)
    compiled = compile_validated_plan(
        plan,
        catalog,
        TableBindings(
            dataset_tables={
                "earthquakes": "earthquake_events",
                "earthquake_scores": "score_lookup",
            }
        ),
    )
    connection = duckdb.connect(":memory:")
    try:
        for statement in setup_sql:
            connection.execute(statement)
        return execute_with_connection(compiled, connection)
    finally:
        connection.close()


@pytest.fixture
def duckdb_setup_sql() -> tuple[str, ...]:
    """Two tiny tables with unique field names for executable Join tests."""

    return (
        """
        CREATE TABLE earthquake_events(
            event_id VARCHAR, event_time TIMESTAMPTZ, latitude DOUBLE,
            longitude DOUBLE, magnitude DOUBLE
        )
        """,
        """
        INSERT INTO earthquake_events VALUES
          ('eq-001', TIMESTAMPTZ '2026-06-01 00:00:00+00:00', 39.9, 116.4, 4.8),
          ('eq-002', TIMESTAMPTZ '2026-06-02 00:00:00+00:00', 40.1, 116.2, 5.1)
        """,
        "CREATE TABLE score_lookup(score_key DOUBLE, severity_label VARCHAR)",
        "INSERT INTO score_lookup VALUES (4.8, 'moderate'), (5.1, 'strong')",
    )


def test_execute_join_uses_two_validated_inputs(
    accept_plan, policy_set, catalog, duckdb_setup_sql
) -> None:
    """The executable Join fragment performs a typed two-input equi-join."""

    raw = copy.deepcopy(accept_plan)
    raw["plan_id"] = "join-execution"
    raw["request_context"]["action"] = "join"
    raw["requested_output"]["fields"] = ["event_id", "severity_label"]
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Project",
            "operator_id": "left-project",
            "inputs": ["op1"],
            "fields": ["event_id", "magnitude"],
        },
        {
            "operator_type": "ScanSource",
            "operator_id": "score-scan",
            "inputs": [],
            "dataset": "earthquake_scores",
            "snapshot": None,
        },
        {
            "operator_type": "Join",
            "operator_id": "score-join",
            "inputs": ["left-project", "score-scan"],
            "left_field": "magnitude",
            "right_field": "score_key",
            "join_type": "inner",
        },
        {
            "operator_type": "Project",
            "operator_id": "join-output",
            "inputs": ["score-join"],
            "fields": ["event_id", "severity_label"],
        },
    ]
    raw["output_operator"] = "join-output"

    result = _execute_real_duckdb(raw, policy_set, catalog, duckdb_setup_sql)

    assert result.rows == (("eq-001", "moderate"), ("eq-002", "strong"))


def test_execute_aggregate_preserves_sql_null_and_type_contracts(
    accept_plan, policy_set, catalog, duckdb_setup_sql
) -> None:
    """COUNT and AVG execute with the aliases and types validated by IR v1."""

    raw = copy.deepcopy(accept_plan)
    raw["plan_id"] = "aggregate-execution"
    raw["request_context"]["action"] = "aggregate"
    raw["requested_output"]["fields"] = ["event_count", "mean_magnitude"]
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Aggregate",
            "operator_id": "aggregate-output",
            "inputs": ["op1"],
            "group_by": [],
            "aggregates": [
                {
                    "function": "count",
                    "input_field": None,
                    "output_field": "event_count",
                },
                {
                    "function": "avg",
                    "input_field": "magnitude",
                    "output_field": "mean_magnitude",
                },
            ],
        },
    ]
    raw["output_operator"] = "aggregate-output"

    result = _execute_real_duckdb(raw, policy_set, catalog, duckdb_setup_sql)

    assert result.rows == ((2, 4.949999999999999),)


def test_execute_numeric_product_aggregate(
    accept_plan, policy_set, catalog, duckdb_setup_sql
) -> None:
    """The bounded arithmetic input compiles to a real DuckDB product."""

    raw = copy.deepcopy(accept_plan)
    raw["plan_id"] = "numeric-product-aggregate"
    raw["request_context"]["action"] = "aggregate"
    raw["requested_output"]["fields"] = ["weighted_value"]
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Aggregate",
            "operator_id": "aggregate-output",
            "inputs": ["op1"],
            "group_by": [],
            "aggregates": [
                {
                    "function": "sum",
                    "input_expression": {
                        "expression_type": "numeric_product",
                        "left": {"expression_type": "field", "field": "magnitude"},
                        "right": {"expression_type": "field", "field": "latitude"},
                    },
                    "output_field": "weighted_value",
                }
            ],
        },
    ]
    raw["output_operator"] = "aggregate-output"

    result = _execute_real_duckdb(raw, policy_set, catalog, duckdb_setup_sql)

    assert result.rows[0][0] == pytest.approx(4.8 * 39.9 + 5.1 * 40.1)


@pytest.mark.parametrize(
    ("method", "expected_first"),
    [
        ("redact", "[REDACTED]"),
        ("hash", hashlib.sha256(b"eq-001").hexdigest()),
        ("null", None),
    ],
)
def test_execute_mask_methods_have_distinct_frozen_results(
    accept_plan, policy_set, catalog, duckdb_setup_sql, method, expected_first
) -> None:
    """Redact, SHA-256, and typed NULL are independent executable contracts."""

    raw = copy.deepcopy(accept_plan)
    raw["plan_id"] = f"mask-{method}-execution"
    raw["operators"].insert(
        1,
        {
            "operator_type": "Mask",
            "operator_id": "mask-event-id",
            "inputs": ["op1"],
            "fields": ["event_id"],
            "method": method,
        },
    )
    raw["operators"][2]["inputs"] = ["mask-event-id"]

    result = _execute_real_duckdb(raw, policy_set, catalog, duckdb_setup_sql)

    assert result.rows[0][0] == expected_first
    assert result.rows[0][1] == 4.8


def test_hash_mask_rejects_non_string_serialization(accept_plan, policy_set, catalog) -> None:
    """V1 does not inherit DuckDB's implicit numeric-to-string hash encoding."""

    raw = copy.deepcopy(accept_plan)
    raw["operators"].insert(
        1,
        {
            "operator_type": "Mask",
            "operator_id": "mask-magnitude",
            "inputs": ["op1"],
            "fields": ["magnitude"],
            "method": "hash",
        },
    )
    raw["operators"][2]["inputs"] = ["mask-magnitude"]

    response = validate(raw, policy_set, catalog)  # type: ignore[arg-type]

    assert response.status == ValidationStatus.REJECT
    assert ReasonCode.MASK_INPUT_TYPE_UNSUPPORTED in {
        diagnostic.code for diagnostic in response.diagnostics
    }


def test_execute_generalize_location_uses_fixed_grid_without_filtering(
    accept_plan, policy_set, catalog, duckdb_setup_sql
) -> None:
    """Generalization changes coordinates but preserves the two input records."""

    raw = copy.deepcopy(accept_plan)
    raw["plan_id"] = "generalize-execution"
    raw["requested_output"]["fields"] = ["latitude", "longitude"]
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "GeneralizeLocation",
            "operator_id": "generalize-grid",
            "inputs": ["op1"],
            "fields": ["latitude", "longitude"],
            "precision_km": 5.0,
            "method": "fixed_grid",
            "preserves_selection": True,
        },
        {
            "operator_type": "Project",
            "operator_id": "grid-output",
            "inputs": ["generalize-grid"],
            "fields": ["latitude", "longitude"],
        },
    ]
    raw["output_operator"] = "grid-output"

    result = _execute_real_duckdb(raw, policy_set, catalog, duckdb_setup_sql)

    step = 5.0 / 111.045
    expected_latitude = ((39.9 + 90.0) // step) * step - 90.0 + step / 2.0
    expected_longitude = ((116.4 + 180.0) // step) * step - 180.0 + step / 2.0
    assert result.row_count == 2
    assert result.rows[0][0] == pytest.approx(expected_latitude)
    assert result.rows[0][1] == pytest.approx(expected_longitude)


def test_execution_digest_preserves_decimal_results_exactly() -> None:
    """Fixed-point monetary aggregates remain serializable and deterministic."""

    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    query = CompiledQuery(
        sql="SELECT SUM(value) AS total FROM (VALUES (1.10::DECIMAL(18,2)), "
        "(2.20::DECIMAL(18,2))) AS amounts(value)",
        parameters=(),
        output_fields=("total",),
        logical_plan_id="decimal-digest-test",
        logical_plan_digest="sha256:test",
    )
    try:
        first = execute_with_connection(query, connection)
        second = execute_with_connection(query, connection)
    finally:
        connection.close()

    assert str(first.rows[0][0]) == "3.30"
    assert first.result_digest == second.result_digest


def test_approved_materialized_plan_compiles_only_with_exact_binding(
    accept_plan, policy_set, catalog
) -> None:
    """Physical IDs and logical digests are checked again at execution time."""

    logical = _validated_plan(accept_plan, policy_set, catalog)
    candidate = generate_duckdb_candidates(
        logical,
        materialization_targets=("op1",),
    )[1]
    bindings = TableBindings(dataset_tables={"earthquakes": "earthquake_events"})

    compiled = compile_approved_physical_plan(logical, candidate, catalog, bindings)

    assert "AS MATERIALIZED" in compiled.sql
    assert compiled.physical_plan_id == candidate.physical_plan_id
    tampered = candidate.model_copy(update={"logical_plan_digest": "sha256:tampered"})
    with pytest.raises(ExecutionCompileError, match="digest does not match"):
        compile_approved_physical_plan(logical, tampered, catalog, bindings)
