"""Execution checks for the bounded early-Mask materialization strategy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import (
    TableBindings,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import Mask, PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

duckdb = pytest.importorskip("duckdb")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_early_mask_boundary_is_approved_equivalent_and_physically_distinct() -> None:
    """Move only a non-key Mask, materialize it, and preserve the final rows."""

    root = Path(__file__).resolve().parents[2]
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_mask_join_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_mask_join_policy.json"))
    response = validate(
        _load_json(examples / "plans/bts_mask_optimizer_transfer.json"),
        policy,
        catalog,
    )
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    logical = response.validated_plan
    mask = next(operator for operator in logical.operators if isinstance(operator, Mask))
    assert mask.fields == ("Tail_Number",)
    candidates = generate_duckdb_candidates(
        logical,
        materialized_operator_placements=((mask.operator_id, "bts-mp-project"),),
    )
    fused, early = candidates
    assert early.strategy.execution_mode == "governance_placed_materialized"

    connection = duckdb.connect()
    try:
        # Small deterministic relations keep this a unit test while exercising
        # the same trusted catalog bindings and DuckDB compiler as real runs.
        connection.execute(
            "CREATE TABLE flights(FlightDate TIMESTAMPTZ, OriginAirportID BIGINT, "
            "Tail_Number VARCHAR, Distance DOUBLE, Cancelled BOOLEAN)"
        )
        connection.execute(
            "INSERT INTO flights VALUES "
            "('2024-01-10 00:00:00+00', 1, 'N001AA', 1000, false), "
            "('2024-01-11 00:00:00+00', 2, 'N002BB', 900, false), "
            "('2024-01-25 00:00:00+00', 1, 'N003CC', 1200, false)"
        )
        connection.execute(
            "CREATE TABLE airports(airport_id BIGINT, airport_code VARCHAR, "
            "city_name VARCHAR, state_code VARCHAR)"
        )
        connection.execute(
            "INSERT INTO airports VALUES (1, 'AAA', 'Alpha', 'AA'), (2, 'BBB', 'Beta', 'BB')"
        )
        bindings = TableBindings(
            dataset_tables={
                "bts_on_time_2024_01_mask_join": "flights",
                "bts_airports_2024_01_mask_join": "airports",
            }
        )
        fused_query = compile_approved_physical_plan(logical, fused, catalog, bindings)
        early_query = compile_approved_physical_plan(logical, early, catalog, bindings)
        fused_result = execute_with_connection(fused_query, connection)
        early_result = execute_with_connection(early_query, connection)
        fused_plan = observe_duckdb_plan(
            connection, fused_query.sql, fused_query.parameters, analyze=False
        )
        early_plan = observe_duckdb_plan(
            connection, early_query.sql, early_query.parameters, analyze=False
        )
    finally:
        connection.close()

    assert "AS MATERIALIZED" in early_query.sql
    assert "AS MATERIALIZED" not in fused_query.sql
    assert 'ORDER BY "Distance" ASC, "airport_code" ASC' in early_query.sql
    assert early_result.columns == fused_result.columns
    assert sorted(early_result.rows) == sorted(fused_result.rows)
    assert all(len(str(row[0])) == 64 for row in early_result.rows)
    assert early_plan.fingerprint != fused_plan.fingerprint


def test_early_mask_cannot_cross_sort_on_the_masked_field() -> None:
    """A disjoint Sort is safe, but sorting Tail_Number blocks Mask pushdown."""

    root = Path(__file__).resolve().parents[2]
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_mask_join_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_mask_join_policy.json"))
    raw = _load_json(examples / "plans/bts_mask_optimizer_transfer.json")
    assert isinstance(raw, dict)
    sort = next(item for item in raw["operators"] if item["operator_type"] == "Sort")
    sort["keys"] = [{"field": "Tail_Number", "direction": "asc"}]
    response = validate(raw, policy, catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    mask = next(
        operator for operator in response.validated_plan.operators if isinstance(operator, Mask)
    )
    with pytest.raises(ValueError, match="Sort that uses the masked field"):
        generate_duckdb_candidates(
            response.validated_plan,
            materialized_operator_placements=((mask.operator_id, "bts-mp-project"),),
        )
