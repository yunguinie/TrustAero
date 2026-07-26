"""Semantic contracts for the bounded TPC-H Query Coverage V2 adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import TableBindings, compile_approved_physical_plan
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import Limit, PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validated(query_id: str):
    examples = PROJECT_ROOT / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_object(examples / "catalog_v2.json")))
    policy = PolicySet.model_validate(_object(examples / "policy_v2.json"))
    response = validate(_object(examples / f"plans/{query_id}.json"), policy, catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    return catalog, response.validated_plan


def test_limit_is_small_and_strict() -> None:
    assert (
        Limit(
            operator_type="Limit", operator_id="top-k", inputs=("sorted",), row_count=10
        ).row_count
        == 10
    )
    with pytest.raises(ValidationError):
        Limit(operator_type="Limit", operator_id="zero", inputs=("sorted",), row_count=0)
    with pytest.raises(ValidationError):
        Limit(
            operator_type="Limit",
            operator_id="unbounded",
            inputs=("sorted",),
            row_count=10_001,
        )


@pytest.mark.parametrize(
    ("query_id", "target", "expected_limit"),
    [("q03", "q03-aggregate", 10), ("q10", "q10-aggregate", 20)],
)
def test_new_adapters_validate_and_compile(
    query_id: str,
    target: str,
    expected_limit: int,
) -> None:
    catalog, validated = _validated(query_id)
    candidates = generate_duckdb_candidates(
        validated,
        materialization_targets=(target,),
    )
    assert len(candidates) == 2
    bindings = TableBindings(
        dataset_tables={
            "tpch_sf1_customer": "customer",
            "tpch_sf1_orders": "orders",
            "tpch_sf1_lineitem_v2": "lineitem",
            "tpch_sf1_nation": "nation",
        }
    )
    compiled = [
        compile_approved_physical_plan(validated, candidate, catalog, bindings)
        for candidate in candidates
    ]

    assert all(" ORDER BY " in query.sql for query in compiled)
    assert all(" LIMIT ?" in query.sql for query in compiled)
    assert all(query.parameters[-1] == expected_limit for query in compiled)
    assert all('? - "l_discount"' in query.sql for query in compiled)
    assert compiled[0].physical_plan_id != compiled[1].physical_plan_id
