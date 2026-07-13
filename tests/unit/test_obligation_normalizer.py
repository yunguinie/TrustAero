"""Merge algebra, conflicts, provenance, and canonical-order tests."""

from __future__ import annotations

import copy
from itertools import permutations
from typing import Any

import pytest

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import (
    ObligationType,
    PolicyDecision,
    ReasonCode,
    ValidationStatus,
)
from trustaero.ir.models import CandidatePlan, GeneralizeLocation, Obligation, PolicySet
from trustaero.policy.evaluator import evaluate_policy
from trustaero.validator.obligation_normalizer import (
    NormalizationStatus,
    normalize_obligations,
)
from trustaero.validator.service import validate


def _obligation(obligation_type: ObligationType, **parameters: Any) -> Obligation:
    return Obligation(obligation_type=obligation_type, parameters=parameters)


def _normalized(*obligations: Obligation) -> tuple[Obligation, ...]:
    result = normalize_obligations(obligations)
    assert result.status == NormalizationStatus.SUCCESS
    assert result.diagnostics == ()
    return result.normalized_obligations


def test_duplicate_version_pin_is_removed() -> None:
    result = normalize_obligations(
        (
            _obligation(ObligationType.VERSION_PIN),
            _obligation(ObligationType.VERSION_PIN),
        ),
        ("policy-b", "policy-a"),
    )

    assert result.normalized_obligations == (_obligation(ObligationType.VERSION_PIN),)
    assert result.provenance[0].source_policy_ids == ("policy-a", "policy-b")
    assert result.provenance[0].rule == "version_pin.deduplicate"


def test_generalization_uses_larger_fixed_grid_cell() -> None:
    result = _normalized(
        _obligation(
            ObligationType.GENERALIZE_LOCATION,
            fields=["longitude", "latitude"],
            precision_km=5,
        ),
        _obligation(
            ObligationType.GENERALIZE_LOCATION,
            fields=["latitude", "longitude"],
            precision_km=10,
            method="fixed_grid",
        ),
    )

    assert result[0].parameters == {
        "fields": ["latitude", "longitude"],
        "precision_km": 10.0,
        "method": "fixed_grid",
    }


def test_minimum_group_size_uses_maximum_requirement() -> None:
    result = _normalized(
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=5),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=20),
    )

    assert result[0].parameters == {"minimum_count": 20}


def test_lineage_uses_strongest_supported_level() -> None:
    result = _normalized(
        _obligation(ObligationType.LINEAGE_CAPTURE, level="none"),
        _obligation(ObligationType.LINEAGE_CAPTURE, level="source"),
        _obligation(ObligationType.LINEAGE_CAPTURE, level="record"),
    )

    assert result[0].parameters == {"level": "record"}


def test_masks_with_same_method_union_fields_and_defaults() -> None:
    result = _normalized(
        _obligation(ObligationType.MASK, fields=["phone"], method="redact"),
        _obligation(ObligationType.MASK, fields=["name"]),
        _obligation(ObligationType.MASK, fields=["name"], method="redact"),
    )

    assert result == (
        _obligation(
            ObligationType.MASK,
            fields=["name", "phone"],
            method="redact",
        ),
    )


def test_disjoint_mask_methods_remain_separate_in_stable_order() -> None:
    result = _normalized(
        _obligation(ObligationType.MASK, fields=["phone"], method="redact"),
        _obligation(ObligationType.MASK, fields=["user_id"], method="hash"),
    )

    assert [item.parameters["method"] for item in result] == ["hash", "redact"]


@pytest.mark.parametrize(
    "obligations",
    [
        (
            _obligation(ObligationType.MASK, fields=["location"], method="hash"),
            _obligation(ObligationType.MASK, fields=["location"], method="redact"),
        ),
        (
            _obligation(ObligationType.MASK, fields=["location"], method="redact"),
            _obligation(ObligationType.MASK, fields=["location"], method="hash"),
        ),
    ],
)
def test_overlapping_incomparable_mask_methods_conflict(
    obligations: tuple[Obligation, ...],
) -> None:
    result = normalize_obligations(obligations)

    assert result.status == NormalizationStatus.CONFLICT
    assert result.normalized_obligations == ()
    assert result.diagnostics[0].code == ReasonCode.MASK_METHOD_CONFLICT


def test_multi_method_conflict_diagnostic_is_permutation_invariant() -> None:
    obligations = (
        _obligation(ObligationType.MASK, fields=["location"], method="hash"),
        _obligation(ObligationType.MASK, fields=["location"], method="redact"),
        _obligation(ObligationType.MASK, fields=["location"], method="null"),
    )
    expected = normalize_obligations(obligations).diagnostics

    for permutation in permutations(obligations):
        assert normalize_obligations(permutation).diagnostics == expected


@pytest.mark.parametrize(
    "obligation",
    [
        _obligation(ObligationType.VERSION_PIN, version="v1"),
        _obligation(ObligationType.MASK, fields=[], method="redact"),
        _obligation(ObligationType.MASK, fields=["name"], method="encrypt"),
        _obligation(
            ObligationType.GENERALIZE_LOCATION,
            fields=["latitude", "longitude"],
            precision_km=True,
        ),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=True),
        _obligation(ObligationType.LINEAGE_CAPTURE, level="field"),
    ],
)
def test_undefined_or_malformed_parameters_fail_closed(obligation: Obligation) -> None:
    result = normalize_obligations((obligation,))

    assert result.status == NormalizationStatus.CONFLICT
    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_PARAMETER_INVALID


def test_normalization_is_permutation_invariant() -> None:
    obligations = (
        _obligation(ObligationType.LINEAGE_CAPTURE, level="source"),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=5),
        _obligation(ObligationType.LINEAGE_CAPTURE, level="record"),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=20),
    )
    expected = _normalized(*obligations)

    for permutation in permutations(obligations):
        assert _normalized(*permutation) == expected


def test_normalization_is_idempotent() -> None:
    once = _normalized(
        _obligation(ObligationType.MASK, fields=["phone"], method="redact"),
        _obligation(ObligationType.MASK, fields=["name"], method="redact"),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=5),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=20),
    )

    assert _normalized(*once) == once


def test_canonical_order_follows_logical_dependency_contract() -> None:
    result = _normalized(
        _obligation(ObligationType.LINEAGE_CAPTURE, level="record"),
        _obligation(ObligationType.MIN_GROUP_SIZE, minimum_count=5),
        _obligation(ObligationType.MASK, fields=["event_id"]),
        _obligation(
            ObligationType.GENERALIZE_LOCATION,
            fields=["latitude", "longitude"],
            precision_km=5,
        ),
        _obligation(ObligationType.VERSION_PIN),
    )

    assert [item.obligation_type for item in result] == [
        ObligationType.VERSION_PIN,
        ObligationType.GENERALIZE_LOCATION,
        ObligationType.MASK,
        ObligationType.MIN_GROUP_SIZE,
        ObligationType.LINEAGE_CAPTURE,
    ]


def test_evaluator_preserves_policy_provenance_until_normalization(
    rewrite_plan: dict[str, Any], policy_set: PolicySet
) -> None:
    plan = policy_set.rules[1]
    duplicated_rule = plan.model_copy(
        update={"policy_id": "P-SECOND", "obligations": plan.obligations[:1]}
    )
    policy = policy_set.model_copy(update={"rules": (*policy_set.rules, duplicated_rule)})

    evaluation = evaluate_policy(CandidatePlan.model_validate(rewrite_plan), policy)
    normalized = normalize_obligations(
        evaluation.obligations,
        evaluation.obligation_sources,
    )

    generalization = next(
        record
        for record in normalized.provenance
        if record.obligation.obligation_type == ObligationType.GENERALIZE_LOCATION
    )
    assert generalization.source_policy_ids == (
        "P-RESEARCH-FACILITY@1",
        "P-SECOND@1",
    )


def test_service_rewrites_once_with_normalized_stronger_obligation(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    base_rule = policy_set.rules[1]
    stronger_rule = base_rule.model_copy(
        update={
            "policy_id": "P-STRONGER-GENERALIZATION",
            "obligations": (
                _obligation(
                    ObligationType.GENERALIZE_LOCATION,
                    fields=["latitude", "longitude"],
                    precision_km=10,
                ),
            ),
        }
    )
    policy = policy_set.model_copy(update={"rules": (*policy_set.rules, stronger_rule)})

    result = validate(copy.deepcopy(rewrite_plan), policy, catalog)

    assert result.status == ValidationStatus.REWRITE
    assert result.validated_plan is not None
    generalizers = [
        operator
        for operator in result.validated_plan.operators
        if isinstance(operator, GeneralizeLocation)
    ]
    assert len(generalizers) == 1
    assert generalizers[0].precision_km == 10


def test_service_result_is_independent_of_policy_rule_order(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    base_rule = policy_set.rules[1]
    stronger_rule = base_rule.model_copy(
        update={
            "policy_id": "P-STRONGER-GENERALIZATION",
            "obligations": (
                _obligation(
                    ObligationType.GENERALIZE_LOCATION,
                    fields=["latitude", "longitude"],
                    precision_km=10,
                ),
            ),
        }
    )
    forward = policy_set.model_copy(update={"rules": (*policy_set.rules, stronger_rule)})
    reversed_rules = policy_set.model_copy(update={"rules": tuple(reversed(forward.rules))})

    first = validate(copy.deepcopy(rewrite_plan), forward, catalog)
    second = validate(copy.deepcopy(rewrite_plan), reversed_rules, catalog)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_not_applicable_rule_cannot_become_implicit_permission(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    non_applicable = policy_set.rules[0].model_copy(
        update={"decision": PolicyDecision.NOT_APPLICABLE}
    )
    policy = policy_set.model_copy(update={"rules": (non_applicable,)})

    result = validate(copy.deepcopy(accept_plan), policy, catalog)

    assert result.status == ValidationStatus.REJECT
    assert result.policy_decision == PolicyDecision.NOT_APPLICABLE
    assert result.diagnostics[0].code == ReasonCode.POLICY_NOT_APPLICABLE


def test_obligations_from_not_applicable_rules_are_ignored(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    permit = policy_set.rules[0]
    non_applicable = permit.model_copy(
        update={
            "policy_id": "P-NOT-APPLICABLE",
            "decision": PolicyDecision.NOT_APPLICABLE,
            # This undefined parameter would fail normalization if the rule's
            # obligations were incorrectly treated as permit requirements.
            "obligations": (_obligation(ObligationType.VERSION_PIN, version="undefined"),),
        }
    )
    policy = policy_set.model_copy(update={"rules": (permit, non_applicable)})

    result = validate(copy.deepcopy(accept_plan), policy, catalog)

    assert result.status == ValidationStatus.ACCEPT
    assert result.policy_decision == PolicyDecision.PERMIT
