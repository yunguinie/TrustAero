"""First 32 deterministic cases used as the seed for the paper workload.

These are not claimed to be a benchmark. They prove that the public validator
contract is predictable across the four TrustAero handling states.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import ObligationType, PolicyDecision, ReasonCode, ValidationStatus
from trustaero.ir.models import GeneralizeLocation, Obligation, PolicySet, SpatialFilter
from trustaero.validator.service import validate

Mutator = Callable[[dict[str, Any]], None]


def noop(_: dict[str, Any]) -> None:
    return


def set_id(value: str) -> Mutator:
    def mutate(raw: dict[str, Any]) -> None:
        raw["plan_id"] = value

    return mutate


ACCEPT_CASES: list[tuple[str, Mutator]] = [
    ("T001_base_accept", noop),
    ("T002_different_id", set_id("pc-a-002")),
    ("T003_explicit_snapshot", lambda p: p["operators"][0].update(snapshot="v2026-06")),
    ("T004_one_output_field", lambda p: p["requested_output"].update(fields=["event_id"])),
    (
        "T005_lineage_requested_but_not_required",
        lambda p: p["requested_output"].update(lineage_level="source"),
    ),
    ("T006_action_aggregate", lambda p: p["request_context"].update(action="aggregate")),
    (
        "T007_subject_attributes",
        lambda p: p["request_context"]["subject"].update(attributes={"org": "ustb"}),
    ),
    (
        "T008_csv_not_requested",
        lambda p: p["requested_output"].update(
            export={"requested": False, "destination": None, "format": "csv"}
        ),
    ),
]

REWRITE_CASES: list[tuple[str, Mutator]] = [
    ("T009_base_rewrite", noop),
    ("T010_different_id", set_id("pc-rw-010")),
    ("T011_explicit_snapshot", lambda p: p["operators"][0].update(snapshot="v2026-06")),
    ("T012_source_lineage_request", lambda p: p["requested_output"].update(lineage_level="source")),
    ("T013_no_lineage_request", lambda p: p["requested_output"].update(lineage_level="none")),
    ("T014_aggregate_action", lambda p: p["request_context"].update(action="aggregate")),
    (
        "T015_subject_attribute",
        lambda p: p["request_context"]["subject"].update(attributes={"clearance": "research"}),
    ),
    ("T016_smaller_radius", lambda p: p["operators"][1].update(radius_km=5)),
]

CLARIFY_CASES: list[tuple[str, Mutator]] = [
    ("T017_base_clarify", noop),
    ("T018_different_id", set_id("pc-cl-018")),
    ("T019_aggregate_without_purpose", lambda p: p["request_context"].update(action="aggregate")),
    (
        "T020_role_change_still_missing_purpose",
        lambda p: p["request_context"]["subject"].update(role="public"),
    ),
    ("T021_lineage_request", lambda p: p["requested_output"].update(lineage_level="record")),
    ("T022_explicit_snapshot", lambda p: p["operators"][0].update(snapshot="v2026-06")),
    (
        "T023_subject_attributes",
        lambda p: p["request_context"]["subject"].update(attributes={"org": "ustb"}),
    ),
    (
        "T024_export_request_without_purpose",
        lambda p: p["requested_output"].update(
            export={"requested": True, "destination": "local", "format": "json"}
        ),
    ),
]


@pytest.mark.parametrize(("case_id", "mutator"), ACCEPT_CASES, ids=[x[0] for x in ACCEPT_CASES])
def test_accept_cases(
    case_id: str,
    mutator: Mutator,
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    raw = copy.deepcopy(accept_plan)
    mutator(raw)
    result = validate(raw, policy_set, catalog)
    assert result.status == ValidationStatus.ACCEPT, case_id
    assert result.validated_plan is not None


@pytest.mark.parametrize(("case_id", "mutator"), REWRITE_CASES, ids=[x[0] for x in REWRITE_CASES])
def test_rewrite_cases(
    case_id: str,
    mutator: Mutator,
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    raw = copy.deepcopy(rewrite_plan)
    mutator(raw)
    result = validate(raw, policy_set, catalog)
    assert result.status == ValidationStatus.REWRITE, case_id
    assert result.validated_plan is not None
    assert {d.code for d in result.diagnostics} == {
        ReasonCode.SPATIAL_PRECISION_EXCEEDED,
        ReasonCode.LINEAGE_REQUIRED,
    }


@pytest.mark.parametrize(("case_id", "mutator"), CLARIFY_CASES, ids=[x[0] for x in CLARIFY_CASES])
def test_clarify_cases(
    case_id: str,
    mutator: Mutator,
    clarify_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    raw = copy.deepcopy(clarify_plan)
    mutator(raw)
    result = validate(raw, policy_set, catalog)
    assert result.status == ValidationStatus.CLARIFY, case_id
    assert result.validated_plan is None
    assert result.diagnostics[0].code == ReasonCode.PURPOSE_MISSING


def reject_mutations() -> list[tuple[str, str, Mutator, ReasonCode]]:
    return [
        ("T025_policy_deny", "reject", noop, ReasonCode.POLICY_DENIED),
        (
            "T026_unknown_dataset",
            "accept",
            lambda p: p["operators"][0].update(dataset="missing"),
            ReasonCode.UNKNOWN_DATASET,
        ),
        (
            "T027_unknown_field",
            "accept",
            lambda p: p["requested_output"].update(fields=["missing"]),
            ReasonCode.UNKNOWN_FIELD,
        ),
        (
            "T028_invalid_snapshot",
            "accept",
            lambda p: p["operators"][0].update(snapshot="v1900"),
            ReasonCode.VERSION_UNRESOLVED,
        ),
        (
            "T029_unbound_reference",
            "accept",
            lambda p: p["operators"][1].update(inputs=["missing"]),
            ReasonCode.UNBOUND_REFERENCE,
        ),
        (
            "T030_cycle",
            "accept",
            lambda p: p["operators"][0].update(inputs=["op2"]),
            ReasonCode.CYCLIC_PLAN,
        ),
        (
            "T031_unknown_operator",
            "accept",
            lambda p: p["operators"][0].update(operator_type="DropDatabase"),
            ReasonCode.UNKNOWN_OPERATOR,
        ),
        (
            "T032_no_applicable_policy",
            "accept",
            lambda p: p["request_context"]["subject"].update(role="visitor"),
            ReasonCode.POLICY_NOT_APPLICABLE,
        ),
    ]


@pytest.mark.parametrize(
    ("case_id", "source", "mutator", "reason"),
    reject_mutations(),
    ids=[x[0] for x in reject_mutations()],
)
def test_reject_cases(
    case_id: str,
    source: str,
    mutator: Mutator,
    reason: ReasonCode,
    accept_plan: dict[str, Any],
    reject_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    raw = copy.deepcopy(reject_plan if source == "reject" else accept_plan)
    mutator(raw)
    result = validate(raw, policy_set, catalog)
    assert result.status == ValidationStatus.REJECT, case_id
    assert result.validated_plan is None
    assert reason in {diagnostic.code for diagnostic in result.diagnostics}


def test_same_input_has_identical_output(
    rewrite_plan: dict[str, Any], policy_set: PolicySet, catalog: InMemoryCatalog
) -> None:
    first = validate(copy.deepcopy(rewrite_plan), policy_set, catalog)
    second = validate(copy.deepcopy(rewrite_plan), policy_set, catalog)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_location_generalization_preserves_query_radius(
    rewrite_plan: dict[str, Any], policy_set: PolicySet, catalog: InMemoryCatalog
) -> None:
    """Output coarsening must not shrink the user's selected geographic area."""

    result = validate(copy.deepcopy(rewrite_plan), policy_set, catalog)
    assert result.validated_plan is not None

    spatial_filter = next(
        operator
        for operator in result.validated_plan.operators
        if isinstance(operator, SpatialFilter)
    )
    generalization = next(
        operator
        for operator in result.validated_plan.operators
        if isinstance(operator, GeneralizeLocation)
    )

    # 50 km decides which rows are selected; 5 km controls only how precisely
    # each selected location may be disclosed in the eventual output.
    assert spatial_filter.radius_km == 50
    assert generalization.precision_km == 5
    assert generalization.fields == ("latitude", "longitude")
    assert generalization.method == "fixed_grid"
    assert generalization.preserves_selection is True


def test_unsupported_obligation_fails_closed(
    accept_plan: dict[str, Any], policy_set: PolicySet, catalog: InMemoryCatalog
) -> None:
    """An unimplemented obligation must never be reported as satisfied."""

    rule = policy_set.rules[0].model_copy(
        update={"obligations": (Obligation(obligation_type=ObligationType.EXPORT_CONTROL),)}
    )
    policy = policy_set.model_copy(update={"rules": (rule, *policy_set.rules[1:])})

    result = validate(copy.deepcopy(accept_plan), policy, catalog)

    assert result.status == ValidationStatus.REJECT
    assert result.policy_decision == PolicyDecision.PERMIT
    assert result.validated_plan is None
    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_CONFLICT
