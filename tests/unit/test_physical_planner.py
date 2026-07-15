"""Minimal physical-plan specification tests."""

from __future__ import annotations

import copy
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import ObligationType, ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.service import validate


def _validated_rewrite_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> ValidatedLogicalPlan:
    response = validate(copy.deepcopy(rewrite_plan), policy_set, catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    return response.validated_plan


def test_physical_plan_binds_to_validated_logical_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    logical = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)

    physical = plan_physical_execution(logical)

    assert physical.logical_plan_id == logical.logical_plan_id
    assert physical.logical_plan_digest == logical.validation.canonical_digest
    assert physical.bindings == logical.bindings
    assert physical.output_operator == f"phys-{logical.output_operator}"


def test_physical_plan_carries_lineage_and_pending_obligations(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    logical = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)

    physical = plan_physical_execution(logical)

    assert physical.lineage_instrumentation == logical.lineage_instrumentation
    assert physical.pending_obligations == (ObligationType.LINEAGE_CAPTURE,)
    assert ObligationType.LINEAGE_CAPTURE not in logical.satisfied_obligations


def test_physical_plan_lists_unimplemented_governance_features(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    logical = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)

    physical = plan_physical_execution(logical)

    assert set(physical.unimplemented_backend_features) >= {
        "fixed_grid_coordinate_transform",
        "lineage_backend_capture",
    }
    by_type = {
        operator.operator_type: operator for operator in physical.physical_operators
    }
    assert by_type["GeneralizeLocation"].implementation_status == "requires_backend"
    assert by_type["LineageCapture"].implementation_status == "requires_backend"


def test_physical_plan_id_is_stable_for_same_logical_plan(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    logical = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)

    first = plan_physical_execution(logical)
    second = plan_physical_execution(logical)

    assert first == second
    assert first.physical_plan_id.startswith("pp-")


def test_duckdb_binding_marks_source_lineage_executable(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """DuckDB binds real V1 features while record lineage remains unsupported."""

    policy_raw = policy_set.model_dump(mode="json")
    policy_raw["rules"][0]["obligations"] = [
        {"obligation_type": "LINEAGE_CAPTURE", "parameters": {"level": "source"}}
    ]
    response = validate(accept_plan, PolicySet.model_validate(policy_raw), catalog)
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None

    physical = plan_physical_execution(response.validated_plan, backend="duckdb")

    by_type = {
        operator.operator_type: operator for operator in physical.physical_operators
    }
    assert by_type["LineageCapture"].implementation_status == "executable"
    assert physical.unimplemented_backend_features == ()


def test_duckdb_binding_keeps_record_lineage_unimplemented(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """Backend binding must not silently downgrade record provenance."""

    logical = _validated_rewrite_plan(rewrite_plan, policy_set, catalog)

    physical = plan_physical_execution(logical, backend="duckdb")

    assert "record_lineage_capture" in physical.unimplemented_backend_features
