"""Type rules for the bounded Filter and Aggregate expression fragment."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import DataType, ReasonCode
from trustaero.ir.models import CandidatePlan
from trustaero.validator.type_checker import type_check_plan


def _comparison(
    field: str,
    operator: str,
    data_type: str,
    value: str | int | float | bool,
) -> dict[str, Any]:
    """Build the explicit IR form used by Filter tests."""

    return {
        "expression_type": "comparison",
        "operator": operator,
        "left": {"expression_type": "field", "field": field},
        "right": {
            "expression_type": "literal",
            "data_type": data_type,
            "value": value,
        },
    }


def _with_terminal_operator(
    accept_plan: dict[str, Any],
    operator: dict[str, Any],
    requested_fields: list[str],
) -> dict[str, Any]:
    """Replace the fixture's Project with one operator after its Scan."""

    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [raw["operators"][0], operator]
    raw["output_operator"] = operator["operator_id"]
    raw["requested_output"]["fields"] = requested_fields
    return raw


def _check(raw: dict[str, Any], catalog: InMemoryCatalog):
    plan = CandidatePlan.model_validate(raw)
    return type_check_plan(plan, catalog)


def test_filter_accepts_numeric_comparison_and_preserves_schema(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter",
            "inputs": ["op1"],
            "expression": _comparison("magnitude", "gt", "float", 5.0),
        },
        ["event_id", "magnitude"],
    )

    result = _check(raw, catalog)

    assert result.diagnostics == ()
    assert result.outputs["op-filter"] == result.outputs["op1"]


def test_filter_checks_every_boolean_operand(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter",
            "inputs": ["op1"],
            "expression": {
                "expression_type": "boolean",
                "operator": "and",
                "operands": [
                    _comparison("magnitude", "ge", "integer", 4),
                    _comparison("missing", "eq", "string", "x"),
                ],
            },
        },
        ["event_id"],
    )

    result = _check(raw, catalog)

    assert {item.code for item in result.diagnostics} == {ReasonCode.FIELD_NOT_AVAILABLE}


@pytest.mark.parametrize(
    ("field", "operator", "literal_type", "value"),
    [
        ("magnitude", "eq", "string", "large"),
        # String ordering is undefined until the IR carries collation metadata.
        ("event_id", "lt", "string", "evt-100"),
    ],
)
def test_filter_rejects_undefined_comparisons(
    accept_plan: dict[str, Any],
    catalog: InMemoryCatalog,
    field: str,
    operator: str,
    literal_type: str,
    value: str | int | float | bool,
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter",
            "inputs": ["op1"],
            "expression": _comparison(field, operator, literal_type, value),
        },
        ["event_id"],
    )

    result = _check(raw, catalog)

    assert result.diagnostics[0].code == ReasonCode.EXPRESSION_TYPE_MISMATCH


def test_literal_declared_type_cannot_disguise_boolean_as_integer(
    accept_plan: dict[str, Any],
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter",
            "inputs": ["op1"],
            "expression": _comparison("magnitude", "eq", "integer", True),
        },
        ["event_id"],
    )

    with pytest.raises(ValidationError, match="declared data_type"):
        CandidatePlan.model_validate(raw)


def test_legacy_free_form_filter_is_rejected_structurally(
    accept_plan: dict[str, Any],
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter",
            "inputs": ["op1"],
            "expression": "magnitude > 5",
        },
        ["event_id"],
    )

    with pytest.raises(ValidationError):
        CandidatePlan.model_validate(raw)


def test_aggregate_derives_named_output_schema(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate",
            "inputs": ["op1"],
            "group_by": ["event_time"],
            "aggregates": [
                {
                    "function": "avg",
                    "input_field": "magnitude",
                    "output_field": "average_magnitude",
                },
                {"function": "count", "output_field": "event_count"},
            ],
        },
        ["event_time", "average_magnitude", "event_count"],
    )

    result = _check(raw, catalog)
    output = result.outputs["op-aggregate"]

    assert result.diagnostics == ()
    assert output.names == ("event_time", "average_magnitude", "event_count")
    assert output.get("average_magnitude").data_type == DataType.FLOAT  # type: ignore[union-attr]
    count = output.get("event_count")
    assert count is not None
    assert count.data_type == DataType.INTEGER
    assert count.nullable is False


def test_aggregate_rejects_unsupported_input_type(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate",
            "inputs": ["op1"],
            "group_by": [],
            "aggregates": [
                {
                    "function": "avg",
                    "input_field": "event_id",
                    "output_field": "invalid_average",
                }
            ],
        },
        ["invalid_average"],
    )

    result = _check(raw, catalog)

    assert result.diagnostics[0].code == ReasonCode.AGGREGATE_TYPE_NOT_SUPPORTED


def test_aggregate_rejects_missing_fields_and_alias_collisions(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    missing = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate",
            "inputs": ["op1"],
            "group_by": ["missing"],
            "aggregates": [{"function": "count", "output_field": "event_count"}],
        },
        ["event_count"],
    )
    collision = _with_terminal_operator(
        accept_plan,
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate",
            "inputs": ["op1"],
            "group_by": ["event_time"],
            "aggregates": [{"function": "count", "output_field": "event_time"}],
        },
        ["event_time"],
    )

    assert _check(missing, catalog).diagnostics[0].code == ReasonCode.FIELD_NOT_AVAILABLE
    assert _check(collision, catalog).diagnostics[0].code == ReasonCode.DUPLICATE_OUTPUT_FIELD


def test_pre_aggregate_field_does_not_survive_without_grouping(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate",
            "inputs": ["op1"],
            "group_by": [],
            "aggregates": [{"function": "count", "output_field": "event_count"}],
        },
        {
            "operator_type": "Project",
            "operator_id": "op-project",
            "inputs": ["op-aggregate"],
            "fields": ["magnitude"],
        },
    ]
    raw["output_operator"] = "op-project"
    raw["requested_output"]["fields"] = ["magnitude"]

    result = _check(raw, catalog)

    assert result.diagnostics[0].code == ReasonCode.FIELD_NOT_AVAILABLE
