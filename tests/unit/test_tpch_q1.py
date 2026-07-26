"""Tests for the bounded official TPC-H Q1 IR extension."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import TableBindings, compile_approved_physical_plan
from trustaero.experiments.tpch_q1 import TPCH_Q1_MATERIALIZATION_TARGETS
from trustaero.ir.enums import ReasonCode, ValidationStatus
from trustaero.ir.models import NumericAffineExpression, PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_numeric_affine_requires_an_exact_decimal_constant() -> None:
    with pytest.raises(ValidationError, match="exact DECIMAL"):
        NumericAffineExpression.model_validate(
            {
                "expression_type": "numeric_affine",
                "constant": {
                    "expression_type": "literal",
                    "data_type": "integer",
                    "value": 1,
                },
                "operator": "subtract",
                "field": {"expression_type": "field", "field": "discount"},
            }
        )


def test_q1_validates_and_compiles_three_reviewed_candidates() -> None:
    examples = PROJECT_ROOT / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_object(examples / "catalog.json")))
    policy = PolicySet.model_validate(_object(examples / "policy.json"))

    response = validate(_object(examples / "plans/q01.json"), policy, catalog)

    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    assert response.validated_plan.output_schema[4].data_type.value == "decimal"
    candidates = generate_duckdb_candidates(
        response.validated_plan,
        materialization_targets=TPCH_Q1_MATERIALIZATION_TARGETS,
    )
    assert len(candidates) == 3
    bindings = TableBindings(dataset_tables={"tpch_sf1_lineitem": "lineitem_q1"})
    compiled = [
        compile_approved_physical_plan(response.validated_plan, item, catalog, bindings)
        for item in candidates
    ]
    assert all('ORDER BY "l_returnflag" ASC, "l_linestatus" ASC' in item.sql for item in compiled)
    assert all('? - "l_discount"' in item.sql for item in compiled)
    assert all('? + "l_tax"' in item.sql for item in compiled)
    assert compiled[0].parameters[:3] == (Decimal("1"), Decimal("1"), Decimal("1"))
    assert "AS MATERIALIZED" not in compiled[0].sql
    assert all("AS MATERIALIZED" in item.sql for item in compiled[1:])


def test_sort_rejects_semantic_reuse_of_a_masked_field() -> None:
    """A presentation token cannot silently become an ordering key."""

    examples = PROJECT_ROOT / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_object(examples / "catalog.json")))
    policy = PolicySet.model_validate(_object(examples / "policy.json"))
    raw = deepcopy(_object(examples / "plans/q01.json"))
    operators = raw["operators"]
    assert isinstance(operators, list)
    operators.insert(
        3,
        {
            "operator_type": "Mask",
            "operator_id": "q01-mask-returnflag",
            "inputs": ["q01-aggregate"],
            "fields": ["l_returnflag"],
            "method": "redact",
        },
    )
    assert isinstance(operators[4], dict)
    operators[4]["inputs"] = ["q01-mask-returnflag"]

    response = validate(raw, policy, catalog)

    assert response.status == ValidationStatus.REJECT
    assert response.diagnostics[0].code == ReasonCode.MASKED_FIELD_USED_SEMANTICALLY
    assert response.diagnostics[0].details["attempted_operation"] == "sort"
