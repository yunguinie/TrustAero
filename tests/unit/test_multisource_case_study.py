"""Focused checks for the four-source evidence and certificate fault gates."""

from __future__ import annotations

from trustaero.experiments.multisource_case_study import (
    _certificate_events,
    _dependency_tampered_events,
    _topological_operators,
)
from trustaero.ir.enums import ReasonCode
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    PhysicalOperatorSpec,
    SnapshotBindings,
)
from trustaero.validator.physical_dag import validate_operator_dependency_events


def _physical_plan() -> ApprovedPhysicalPlan:
    return ApprovedPhysicalPlan(
        physical_plan_id="physical-test",
        logical_plan_id="logical-test",
        logical_plan_digest="sha256:logical",
        output_operator="pp-project",
        physical_operators=(
            PhysicalOperatorSpec(
                physical_operator_id="pp-project",
                logical_operator_id="project",
                operator_type="Project",
                inputs=("pp-join",),
                backend="duckdb",
                implementation_status="executable",
            ),
            PhysicalOperatorSpec(
                physical_operator_id="pp-right",
                logical_operator_id="right",
                operator_type="ScanSource",
                backend="duckdb",
                implementation_status="executable",
            ),
            PhysicalOperatorSpec(
                physical_operator_id="pp-left",
                logical_operator_id="left",
                operator_type="ScanSource",
                backend="duckdb",
                implementation_status="executable",
            ),
            PhysicalOperatorSpec(
                physical_operator_id="pp-join",
                logical_operator_id="join",
                operator_type="SpatialJoin",
                inputs=("pp-left", "pp-right"),
                backend="duckdb",
                implementation_status="executable",
            ),
        ),
        bindings=SnapshotBindings(
            policy_snapshot="policy:test",
            data_snapshots={"left": "sha256:left", "right": "sha256:right"},
        ),
    )


def test_topological_events_respect_branching_dependencies() -> None:
    physical = _physical_plan()

    ordered = _topological_operators(physical)
    positions = {operator.physical_operator_id: index for index, operator in enumerate(ordered)}

    assert positions["pp-left"] < positions["pp-join"]
    assert positions["pp-right"] < positions["pp-join"]
    assert positions["pp-join"] < positions["pp-project"]


def test_dependency_fault_keeps_sequences_valid_but_breaks_dag_order() -> None:
    physical = _physical_plan()
    events = _certificate_events(
        physical,
        policy_snapshot="policy:test",
        result_digest="sha256:result",
        lineage_digest="sha256:lineage",
    )

    tampered = _dependency_tampered_events(physical, events)
    diagnostics = validate_operator_dependency_events(physical, tampered)

    assert [event.sequence for event in tampered] == list(range(len(tampered)))
    assert ReasonCode.CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION in {
        item.code for item in diagnostics
    }
