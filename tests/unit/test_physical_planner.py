"""Minimal physical-plan specification tests."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import ObligationType, ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
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


def _validated_accept_plan(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> ValidatedLogicalPlan:
    response = validate(copy.deepcopy(accept_plan), policy_set, catalog)
    assert response.status == ValidationStatus.ACCEPT
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
    by_type = {operator.operator_type: operator for operator in physical.physical_operators}
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

    by_type = {operator.operator_type: operator for operator in physical.physical_operators}
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


def test_candidate_generator_binds_distinct_strategies_to_one_validated_plan(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """Fused and materialized choices are auditable physical candidates."""

    logical = _validated_accept_plan(accept_plan, policy_set, catalog)

    candidates = generate_duckdb_candidates(
        logical,
        materialization_targets=("op1", "op1"),
    )

    assert len(candidates) == 2
    assert {candidate.strategy.execution_mode for candidate in candidates} == {
        "fused",
        "materialized",
    }
    assert len({candidate.physical_plan_id for candidate in candidates}) == 2
    assert all(candidate.logical_plan_id == logical.logical_plan_id for candidate in candidates)
    assert all(
        candidate.logical_plan_digest == logical.validation.canonical_digest
        for candidate in candidates
    )
    assert all(candidate.bindings == logical.bindings for candidate in candidates)


def test_materialization_candidate_rejects_unknown_or_final_target(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """A physical decision cannot refer outside the validated logical graph."""

    logical = _validated_accept_plan(accept_plan, policy_set, catalog)

    with pytest.raises(ValueError, match="not in the logical plan"):
        generate_duckdb_candidates(logical, materialization_targets=("missing",))
    with pytest.raises(ValueError, match="final output"):
        generate_duckdb_candidates(
            logical,
            materialization_targets=(logical.output_operator,),
        )
