"""Tests for the minimal trusted execution compiler."""

from __future__ import annotations

import copy
import importlib.util
from typing import Any

import pytest

from trustaero.execution import (
    DuckDBUnavailable,
    ExecutionCompileError,
    TableBindings,
    compile_validated_plan,
    execute_with_connection,
    execute_with_duckdb,
)
from trustaero.ir.enums import DataType, ValidationStatus
from trustaero.ir.models import LiteralExpression
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


def test_compile_rejects_governance_operators_until_backend_semantics_exist(
    rewrite_plan, policy_set, catalog
) -> None:
    """Rewritten governance operators are not silently treated as executable."""

    plan = _validated_plan(rewrite_plan, policy_set, catalog)

    with pytest.raises(ExecutionCompileError, match="not executable"):
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
