"""Tests for explicit, fail-closed point-distance SpatialJoin execution."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import (
    TableBindings,
    compile_validated_plan,
    execute_with_connection,
)
from trustaero.ir.enums import ReasonCode, ValidationStatus
from trustaero.ir.models import CandidatePlan, PolicySet
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.service import validate
from trustaero.validator.type_checker import type_check_plan


def _dataset(
    dataset_id: str,
    identifier: str,
    latitude: str,
    longitude: str,
) -> dict[str, Any]:
    """Build one tiny catalog source with uniquely named point coordinates."""

    return {
        "dataset_id": dataset_id,
        "versions": ["fixture-v1"],
        "default_version": "fixture-v1",
        "fields": [
            {"name": identifier, "data_type": "string", "roles": ["identifier"]},
            {"name": latitude, "data_type": "float", "roles": ["spatial"]},
            {"name": longitude, "data_type": "float", "roles": ["spatial"]},
        ],
        "spatial": {
            "latitude_field": latitude,
            "longitude_field": longitude,
            "crs": "EPSG:4326",
        },
        "temporal_field": None,
    }


@pytest.fixture
def spatial_catalog() -> InMemoryCatalog:
    return InMemoryCatalog(
        CatalogDocument.model_validate(
            {
                "schema_version": "1.0",
                "datasets": [
                    _dataset(
                        "events",
                        "event_id",
                        "earthquake_latitude",
                        "earthquake_longitude",
                    ),
                    _dataset("wells", "well_id", "well_latitude", "well_longitude"),
                    _dataset(
                        "airports",
                        "airport_id",
                        "airport_latitude",
                        "airport_longitude",
                    ),
                ],
            }
        )
    )


@pytest.fixture
def spatial_policy() -> PolicySet:
    return PolicySet.model_validate(
        {
            "schema_version": "1.0",
            "policy_set_id": "spatial-fixture-policy",
            "policy_snapshot": "fixture-policy-v1",
            "rules": [
                {
                    "policy_id": "permit-spatial-join",
                    "policy_version": "1",
                    "subject_roles": ["researcher"],
                    "purposes": ["research"],
                    "actions": ["join"],
                    "resources": ["events", "wells", "airports"],
                    "decision": "PERMIT",
                    "obligations": [],
                    "reason": "Fixture permits the reviewed spatial join.",
                }
            ],
        }
    )


def _spatial_plan() -> dict[str, Any]:
    """Create two joins where the second must select one of two left pairs."""

    return {
        "schema_version": "1.0",
        "plan_id": "spatial-join-fixture",
        "request_context": {
            "subject": {
                "subject_id": "fixture-user",
                "role": "researcher",
                "attributes": {},
            },
            "purpose": "research",
            "action": "join",
            "query_time_window": None,
        },
        "requested_output": {
            "fields": ["event_id", "well_id", "airport_id"],
            "export": {"requested": False, "destination": None, "format": None},
            "lineage_level": "none",
        },
        "operators": [
            {
                "operator_type": "ScanSource",
                "operator_id": "event-scan",
                "inputs": [],
                "dataset": "events",
                "snapshot": None,
            },
            {
                "operator_type": "ScanSource",
                "operator_id": "well-scan",
                "inputs": [],
                "dataset": "wells",
                "snapshot": None,
            },
            {
                "operator_type": "SpatialJoin",
                "operator_id": "event-well-join",
                "inputs": ["event-scan", "well-scan"],
                "relation": "distance_within",
                "left_fields": [
                    "earthquake_latitude",
                    "earthquake_longitude",
                ],
                "right_fields": ["well_latitude", "well_longitude"],
                "distance_km": 30.0,
            },
            {
                "operator_type": "ScanSource",
                "operator_id": "airport-scan",
                "inputs": [],
                "dataset": "airports",
                "snapshot": None,
            },
            {
                "operator_type": "SpatialJoin",
                "operator_id": "event-airport-join",
                "inputs": ["event-well-join", "airport-scan"],
                "relation": "distance_within",
                "left_fields": [
                    "earthquake_latitude",
                    "earthquake_longitude",
                ],
                "right_fields": ["airport_latitude", "airport_longitude"],
                "distance_km": 30.0,
            },
            {
                "operator_type": "Project",
                "operator_id": "result-project",
                "inputs": ["event-airport-join"],
                "fields": ["event_id", "well_id", "airport_id"],
            },
        ],
        "output_operator": "result-project",
    }


def test_spatial_join_requires_distance_for_distance_relation() -> None:
    raw = _spatial_plan()
    del raw["operators"][2]["distance_km"]

    with pytest.raises(ValidationError, match="distance_within requires"):
        CandidatePlan.model_validate(raw)


def test_spatial_join_rejects_pair_not_declared_by_catalog(
    spatial_catalog: InMemoryCatalog,
) -> None:
    raw = _spatial_plan()
    raw["operators"][2]["left_fields"] = [
        "earthquake_longitude",
        "earthquake_latitude",
    ]
    result = type_check_plan(CandidatePlan.model_validate(raw), spatial_catalog)

    assert result.diagnostics[0].code == ReasonCode.SPATIAL_DESCRIPTOR_NOT_FOUND
    assert result.diagnostics[0].operator_id == "event-well-join"


def test_spatial_join_executes_haversine_and_selects_explicit_left_pair(
    spatial_catalog: InMemoryCatalog,
    spatial_policy: PolicySet,
) -> None:
    duckdb = pytest.importorskip("duckdb")
    response = validate(_spatial_plan(), spatial_policy, spatial_catalog)
    assert response.status == ValidationStatus.ACCEPT
    assert response.validated_plan is not None
    physical = plan_physical_execution(response.validated_plan, backend="duckdb")
    assert physical.unimplemented_backend_features == ()

    compiled = compile_validated_plan(
        response.validated_plan,
        spatial_catalog,
        TableBindings(
            dataset_tables={
                "events": "event_table",
                "wells": "well_table",
                "airports": "airport_table",
            }
        ),
    )
    assert compiled.parameters == (30.0, 30.0, 30.0, 30.0)
    assert "6371.0088" in compiled.sql
    assert "abs(" in compiled.sql

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE event_table(
                event_id VARCHAR,
                earthquake_latitude DOUBLE,
                earthquake_longitude DOUBLE
            );
            INSERT INTO event_table VALUES ('event-near', 42.65, -73.75);
            CREATE TABLE well_table(
                well_id VARCHAR, well_latitude DOUBLE, well_longitude DOUBLE
            );
            INSERT INTO well_table VALUES
                ('well-near', 42.66, -73.76),
                ('well-far', 44.0, -75.0);
            CREATE TABLE airport_table(
                airport_id VARCHAR,
                airport_latitude DOUBLE,
                airport_longitude DOUBLE
            );
            INSERT INTO airport_table VALUES
                ('airport-near', 42.70, -73.80),
                ('airport-far', 43.5, -76.0);
            """
        )
        result = execute_with_connection(compiled, connection)
    finally:
        connection.close()

    assert result.rows == (("event-near", "well-near", "airport-near"),)


def test_spatial_join_unsupported_relation_fails_at_execution_boundary(
    spatial_catalog: InMemoryCatalog,
    spatial_policy: PolicySet,
) -> None:
    raw = copy.deepcopy(_spatial_plan())
    raw["operators"][2]["relation"] = "within"
    raw["operators"][2]["distance_km"] = None
    response = validate(raw, spatial_policy, spatial_catalog)
    assert response.status == ValidationStatus.ACCEPT
    assert response.validated_plan is not None
    physical = plan_physical_execution(response.validated_plan, backend="duckdb")
    assert physical.unimplemented_backend_features == ("duckdb_spatial_join_within",)

    from trustaero.execution import ExecutionCompileError

    with pytest.raises(ExecutionCompileError, match="distance_within"):
        compile_validated_plan(
            response.validated_plan,
            spatial_catalog,
            TableBindings(
                dataset_tables={
                    "events": "event_table",
                    "wells": "well_table",
                    "airports": "airport_table",
                }
            ),
        )
