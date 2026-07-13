"""L2 relation-schema propagation and operator transfer-rule tests."""

from __future__ import annotations

import copy
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import ObligationType, ReasonCode, ValidationStatus
from trustaero.ir.models import CandidatePlan, Obligation, PolicySet
from trustaero.validator.service import validate
from trustaero.validator.type_checker import type_check_plan


def _codes(raw: dict[str, Any], catalog: InMemoryCatalog) -> set[ReasonCode]:
    plan = CandidatePlan.model_validate(raw)
    return {diagnostic.code for diagnostic in type_check_plan(plan, catalog).diagnostics}


def test_scan_and_project_infer_final_schema(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    plan = CandidatePlan.model_validate(accept_plan)
    result = type_check_plan(plan, catalog)

    assert result.diagnostics == ()
    assert result.outputs["op1"].names == (
        "event_id",
        "event_time",
        "latitude",
        "longitude",
        "magnitude",
    )
    assert result.outputs["op2"].names == ("event_id", "magnitude")
    assert result.outputs["op2"].spatial == ()


def test_project_rejects_missing_field(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"][1]["fields"] = ["event_id", "missing"]
    raw["requested_output"]["fields"] = ["event_id"]

    assert ReasonCode.FIELD_NOT_AVAILABLE in _codes(raw, catalog)


def test_spatial_filter_rejects_coordinates_removed_by_project(
    rewrite_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(rewrite_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Project",
            "operator_id": "op2",
            "inputs": ["op1"],
            "fields": ["facility_id"],
        },
        {
            "operator_type": "SpatialFilter",
            "operator_id": "op3",
            "inputs": ["op2"],
            "center": [116.3, 39.9],
            "radius_km": 50,
            "crs": "EPSG:4326",
        },
    ]
    raw["output_operator"] = "op3"
    raw["requested_output"]["fields"] = ["facility_id"]

    assert ReasonCode.SPATIAL_FIELD_REQUIRED in _codes(raw, catalog)


def test_temporal_filter_accepts_catalog_temporal_field(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"].insert(
        1,
        {
            "operator_type": "TemporalFilter",
            "operator_id": "op-time",
            "inputs": ["op1"],
            "field": "event_time",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
    )
    raw["operators"][2]["inputs"] = ["op-time"]

    assert _codes(raw, catalog) == set()


def test_temporal_filter_rejects_non_temporal_type(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"].insert(
        1,
        {
            "operator_type": "TemporalFilter",
            "operator_id": "op-time",
            "inputs": ["op1"],
            "field": "magnitude",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
    )
    raw["operators"][2]["inputs"] = ["op-time"]

    assert ReasonCode.TEMPORAL_FIELD_TYPE_INVALID in _codes(raw, catalog)


def test_temporal_filter_rejects_reversed_interval(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"].insert(
        1,
        {
            "operator_type": "TemporalFilter",
            "operator_id": "op-time",
            "inputs": ["op1"],
            "field": "event_time",
            "start": "2026-02-01T00:00:00Z",
            "end": "2026-01-01T00:00:00Z",
        },
    )
    raw["operators"][2]["inputs"] = ["op-time"]

    assert ReasonCode.INVALID_TIME_RANGE in _codes(raw, catalog)


def test_join_accepts_disjoint_outputs_with_equal_key_types(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "ScanSource",
            "operator_id": "op2",
            "inputs": [],
            "dataset": "critical_facilities",
        },
        {
            "operator_type": "Project",
            "operator_id": "op3",
            "inputs": ["op1"],
            "fields": ["event_id", "magnitude"],
        },
        {
            "operator_type": "Project",
            "operator_id": "op4",
            "inputs": ["op2"],
            "fields": ["facility_id", "facility_type"],
        },
        {
            "operator_type": "Join",
            "operator_id": "op5",
            "inputs": ["op3", "op4"],
            "left_field": "event_id",
            "right_field": "facility_id",
        },
    ]
    raw["output_operator"] = "op5"
    raw["requested_output"]["fields"] = ["event_id", "facility_id"]

    assert _codes(raw, catalog) == set()


def test_join_rejects_incompatible_key_types(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "ScanSource",
            "operator_id": "op2",
            "inputs": [],
            "dataset": "critical_facilities",
        },
        {
            "operator_type": "Project",
            "operator_id": "op3",
            "inputs": ["op1"],
            "fields": ["magnitude"],
        },
        {
            "operator_type": "Project",
            "operator_id": "op4",
            "inputs": ["op2"],
            "fields": ["facility_id"],
        },
        {
            "operator_type": "Join",
            "operator_id": "op5",
            "inputs": ["op3", "op4"],
            "left_field": "magnitude",
            "right_field": "facility_id",
        },
    ]
    raw["output_operator"] = "op5"
    raw["requested_output"]["fields"] = ["magnitude"]

    assert ReasonCode.JOIN_KEY_TYPE_MISMATCH in _codes(raw, catalog)


def test_requested_field_must_survive_to_final_output(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["requested_output"]["fields"] = ["event_time"]

    assert ReasonCode.FIELD_NOT_AVAILABLE in _codes(raw, catalog)


def test_rewrite_is_rechecked_against_relation_schema(
    rewrite_plan: dict[str, Any], policy_set: PolicySet, catalog: InMemoryCatalog
) -> None:
    """A policy-generated operator does not bypass the same binding rules."""

    rule = policy_set.rules[1].model_copy(
        update={
            "obligations": (
                Obligation(
                    obligation_type=ObligationType.GENERALIZE_LOCATION,
                    parameters={
                        "fields": ["missing"],
                        "precision_km": 5.0,
                        "method": "fixed_grid",
                    },
                ),
            )
        }
    )
    policy = policy_set.model_copy(
        update={"rules": (policy_set.rules[0], rule, policy_set.rules[2])}
    )

    result = validate(copy.deepcopy(rewrite_plan), policy, catalog)

    assert result.status == ValidationStatus.REJECT
    assert result.validated_plan is None
    assert result.diagnostics[0].code == ReasonCode.FIELD_NOT_AVAILABLE


def test_mask_strips_identifier_capability_before_join(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "ScanSource",
            "operator_id": "op-facilities",
            "inputs": [],
            "dataset": "critical_facilities",
        },
        {
            "operator_type": "Mask",
            "operator_id": "op-mask",
            "inputs": ["op1"],
            "fields": ["event_id"],
            "method": "hash",
        },
        {
            "operator_type": "Project",
            "operator_id": "op-facility-project",
            "inputs": ["op-facilities"],
            "fields": ["facility_id"],
        },
        {
            "operator_type": "Join",
            "operator_id": "op-join",
            "inputs": ["op-mask", "op-facility-project"],
            "left_field": "event_id",
            "right_field": "facility_id",
        },
    ]
    raw["output_operator"] = "op-join"
    raw["requested_output"]["fields"] = ["event_id", "facility_id"]

    assert ReasonCode.MASKED_FIELD_USED_SEMANTICALLY in _codes(raw, catalog)


def test_mask_strips_spatial_capability_before_spatial_filter(
    rewrite_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(rewrite_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Mask",
            "operator_id": "op-mask",
            "inputs": ["op1"],
            "fields": ["latitude"],
            "method": "redact",
        },
        {
            "operator_type": "SpatialFilter",
            "operator_id": "op-spatial",
            "inputs": ["op-mask"],
            "center": [116.3, 39.9],
            "radius_km": 50,
            "crs": "EPSG:4326",
        },
    ]
    raw["output_operator"] = "op-spatial"
    raw["requested_output"]["fields"] = ["facility_id"]

    assert ReasonCode.SPATIAL_FIELD_REQUIRED in _codes(raw, catalog)


def test_null_masked_temporal_field_cannot_drive_temporal_filter(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Mask",
            "operator_id": "op-mask",
            "inputs": ["op1"],
            "fields": ["event_time"],
            "method": "null",
        },
        {
            "operator_type": "TemporalFilter",
            "operator_id": "op-time",
            "inputs": ["op-mask"],
            "field": "event_time",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
    ]
    raw["output_operator"] = "op-time"
    raw["requested_output"]["fields"] = ["event_id"]

    assert ReasonCode.TEMPORAL_FIELD_TYPE_INVALID in _codes(raw, catalog)


def test_masked_fields_remain_projectable_with_explicit_state(
    accept_plan: dict[str, Any], catalog: InMemoryCatalog
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Mask",
            "operator_id": "op-mask",
            "inputs": ["op1"],
            "fields": ["event_id", "magnitude", "event_time"],
            "method": "redact",
        },
        {
            "operator_type": "Project",
            "operator_id": "op-project",
            "inputs": ["op-mask"],
            "fields": ["event_id", "magnitude", "event_time"],
        },
    ]
    raw["output_operator"] = "op-project"
    raw["requested_output"]["fields"] = ["event_id", "magnitude", "event_time"]

    result = type_check_plan(CandidatePlan.model_validate(raw), catalog)

    assert result.diagnostics == ()
    output = result.outputs["op-project"]
    event_id = output.get("event_id")
    magnitude = output.get("magnitude")
    event_time = output.get("event_time")
    assert event_id is not None
    assert magnitude is not None
    assert event_time is not None
    assert event_id.data_type.value == "string"
    assert magnitude.data_type.value == "string"
    assert event_time.value_state == "redacted"
    assert event_time.roles == frozenset()
