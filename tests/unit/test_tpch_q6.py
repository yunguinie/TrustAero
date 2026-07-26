"""Semantic construction tests for the first governed official TPC-H query."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import TableBindings, compile_approved_physical_plan
from trustaero.experiments.tpch_q6 import TPCH_Q6_MATERIALIZATION_TARGETS
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import AggregateExpression, LiteralExpression, PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_numeric_product_model_is_deliberately_narrow() -> None:
    product = {
        "expression_type": "numeric_product",
        "left": {"expression_type": "field", "field": "a"},
        "right": {"expression_type": "field", "field": "b"},
    }
    with pytest.raises(ValidationError, match="COUNT"):
        AggregateExpression.model_validate(
            {"function": "count", "input_expression": product, "output_field": "n"}
        )
    with pytest.raises(ValidationError, match="exactly one"):
        AggregateExpression.model_validate(
            {
                "function": "sum",
                "input_field": "a",
                "input_expression": product,
                "output_field": "n",
            }
        )


def test_decimal_literals_require_canonical_strings() -> None:
    literal = LiteralExpression.model_validate(
        {"expression_type": "literal", "data_type": "decimal", "value": "0.05"}
    )

    assert literal.value == "0.05"
    for invalid in (0.05, "5e-2", "+0.05", "00.05"):
        with pytest.raises(ValidationError):
            LiteralExpression.model_validate(
                {"expression_type": "literal", "data_type": "decimal", "value": invalid}
            )


def test_q6_validates_and_compiles_three_reviewed_candidates() -> None:
    examples = PROJECT_ROOT / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_object(examples / "catalog.json")))
    policy = PolicySet.model_validate(_object(examples / "policy.json"))

    response = validate(_object(examples / "plans/q06.json"), policy, catalog)

    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    assert response.validated_plan.output_schema[0].data_type.value == "decimal"
    candidates = generate_duckdb_candidates(
        response.validated_plan,
        materialization_targets=TPCH_Q6_MATERIALIZATION_TARGETS,
    )
    assert len(candidates) == 3
    bindings = TableBindings(dataset_tables={"tpch_sf1_lineitem": "lineitem_q6"})
    compiled = [
        compile_approved_physical_plan(response.validated_plan, item, catalog, bindings)
        for item in candidates
    ]
    assert all('SUM(("l_extendedprice" * "l_discount"))' in item.sql for item in compiled)
    assert compiled[0].parameters[-3:] == (
        Decimal("0.05"),
        Decimal("0.07"),
        Decimal("24"),
    )
    assert "AS MATERIALIZED" not in compiled[0].sql
    assert all("AS MATERIALIZED" in item.sql for item in compiled[1:])
