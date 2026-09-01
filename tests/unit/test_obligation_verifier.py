"""Independent governance-obligation postcondition tests."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.ir.enums import (
    LineageLevel,
    ObligationType,
    ReasonCode,
    ValidationStatus,
)
from trustaero.ir.models import (
    Aggregate,
    CandidatePlan,
    GeneralizeLocation,
    LineageCapture,
    LineageEvidenceSummary,
    LineageInstrumentationSpec,
    LineageRequirement,
    Mask,
    MinGroupSize,
    Obligation,
    Operator,
    PolicySet,
    Project,
    ScanSource,
)
from trustaero.validator import service
from trustaero.validator.lineage import verify_lineage_evidence
from trustaero.validator.obligations import verify_obligations


def _base_operators() -> tuple[Operator, ...]:
    return (
        ScanSource(
            operator_type="ScanSource",
            operator_id="scan",
            dataset="earthquakes",
        ),
        Project(
            operator_type="Project",
            operator_id="candidate-output",
            inputs=("scan",),
            fields=("event_id", "latitude", "longitude"),
        ),
    )


def test_matching_enforcers_after_boundary_satisfy_obligations() -> None:
    operators = _base_operators() + (
        GeneralizeLocation(
            operator_type="GeneralizeLocation",
            operator_id="generalize",
            inputs=("candidate-output",),
            fields=("latitude", "longitude"),
            precision_km=10,
        ),
        LineageCapture(
            operator_type="LineageCapture",
            operator_id="lineage",
            inputs=("generalize",),
            level=LineageLevel.RECORD,
        ),
    )
    obligations = (
        Obligation(
            obligation_type=ObligationType.GENERALIZE_LOCATION,
            parameters={"fields": ["latitude", "longitude"], "precision_km": 5},
        ),
        Obligation(
            obligation_type=ObligationType.LINEAGE_CAPTURE,
            parameters={"level": "source"},
        ),
    )

    result = verify_obligations(
        obligations,
        operators,
        boundary_operator="candidate-output",
        output_operator="lineage",
        data_snapshots={"earthquakes": "v2026-06"},
    )

    # Ten-kilometre cells are at least as privacy-preserving as the required
    # five kilometres. Lineage is planned but remains pending until execution
    # evidence is checked.
    assert result.diagnostics == ()
    assert result.satisfied == (ObligationType.GENERALIZE_LOCATION,)
    assert result.pending == (ObligationType.LINEAGE_CAPTURE,)
    assert result.lineage_requirements == (
        LineageRequirement(level=LineageLevel.SOURCE, target_operator="candidate-output"),
    )
    assert result.lineage_instrumentation == (
        LineageInstrumentationSpec(
            level=LineageLevel.RECORD,
            target_operator="candidate-output",
            capture_operator="lineage",
        ),
    )


def test_operator_outside_post_output_suffix_cannot_claim_satisfaction() -> None:
    operators = _base_operators() + (
        Mask(
            operator_type="Mask",
            operator_id="unused-mask",
            inputs=("candidate-output",),
            fields=("event_id",),
        ),
    )
    obligation = Obligation(
        obligation_type=ObligationType.MASK,
        parameters={"fields": ["event_id"]},
    )

    result = verify_obligations(
        (obligation,),
        operators,
        boundary_operator="candidate-output",
        # The final output bypasses unused-mask, so masking was not enforced.
        output_operator="candidate-output",
        data_snapshots={"earthquakes": "v2026-06"},
    )

    assert result.satisfied == ()
    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_NOT_ENFORCED


def test_minimum_group_size_must_meet_required_strength() -> None:
    operators = _base_operators() + (
        MinGroupSize(
            operator_type="MinGroupSize",
            operator_id="minimum-group",
            inputs=("candidate-output",),
            minimum_count=4,
        ),
    )
    obligation = Obligation(
        obligation_type=ObligationType.MIN_GROUP_SIZE,
        parameters={"minimum_count": 5},
    )

    result = verify_obligations(
        (obligation,),
        operators,
        boundary_operator="candidate-output",
        output_operator="minimum-group",
        data_snapshots={"earthquakes": "v2026-06"},
    )

    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_NOT_ENFORCED


def test_rewritten_output_must_descend_from_original_boundary() -> None:
    operators = _base_operators() + (
        Mask(
            operator_type="Mask",
            operator_id="wrong-branch",
            inputs=("scan",),
            fields=("event_id",),
        ),
    )

    result = verify_obligations(
        (),
        operators,
        boundary_operator="candidate-output",
        output_operator="wrong-branch",
        data_snapshots={"earthquakes": "v2026-06"},
    )

    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_NOT_ENFORCED
    assert "unary enforcement chain" in result.diagnostics[0].message


def test_version_pin_requires_resolved_binding_for_every_scan() -> None:
    obligation = Obligation(obligation_type=ObligationType.VERSION_PIN)

    missing = verify_obligations(
        (obligation,),
        _base_operators(),
        boundary_operator="candidate-output",
        output_operator="candidate-output",
        data_snapshots={},
    )
    resolved = verify_obligations(
        (obligation,),
        _base_operators(),
        boundary_operator="candidate-output",
        output_operator="candidate-output",
        data_snapshots={"earthquakes": "v2026-06"},
    )

    assert missing.diagnostics[0].code == ReasonCode.OBLIGATION_NOT_ENFORCED
    assert resolved.diagnostics == ()
    assert resolved.satisfied == (ObligationType.VERSION_PIN,)


def test_lineage_requirement_needs_instrumentation_on_output_suffix() -> None:
    obligation = Obligation(
        obligation_type=ObligationType.LINEAGE_CAPTURE,
        parameters={"level": "record"},
    )

    result = verify_obligations(
        (obligation,),
        _base_operators(),
        boundary_operator="candidate-output",
        output_operator="candidate-output",
        data_snapshots={"earthquakes": "v2026-06"},
    )

    assert result.satisfied == ()
    assert result.pending == ()
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_INSTRUMENTATION_MISSING


def test_lineage_evidence_record_satisfies_source_requirement() -> None:
    result = verify_lineage_evidence(
        (
            LineageRequirement(
                level=LineageLevel.SOURCE,
                target_operator="candidate-output",
            ),
        ),
        (
            LineageInstrumentationSpec(
                level=LineageLevel.RECORD,
                target_operator="candidate-output",
                capture_operator="lineage",
            ),
        ),
        LineageEvidenceSummary(
            execution_id="exec-1",
            result_id="result-1",
            lineage_level=LineageLevel.RECORD,
            covered_operators=("candidate-output",),
            edge_digest="sha256:abc",
        ),
    )

    assert result.satisfied is True
    assert result.diagnostics == ()


def test_lineage_evidence_source_cannot_satisfy_record_requirement() -> None:
    result = verify_lineage_evidence(
        (
            LineageRequirement(
                level=LineageLevel.RECORD,
                target_operator="candidate-output",
            ),
        ),
        (
            LineageInstrumentationSpec(
                level=LineageLevel.RECORD,
                target_operator="candidate-output",
                capture_operator="lineage",
            ),
        ),
        LineageEvidenceSummary(
            execution_id="exec-1",
            result_id="result-1",
            lineage_level=LineageLevel.SOURCE,
            covered_operators=("candidate-output",),
            edge_digest="sha256:abc",
        ),
    )

    assert result.satisfied is False
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_LEVEL_INSUFFICIENT


def test_lineage_evidence_must_cover_required_target() -> None:
    result = verify_lineage_evidence(
        (
            LineageRequirement(
                level=LineageLevel.SOURCE,
                target_operator="candidate-output",
            ),
        ),
        (
            LineageInstrumentationSpec(
                level=LineageLevel.SOURCE,
                target_operator="candidate-output",
                capture_operator="lineage",
            ),
        ),
        LineageEvidenceSummary(
            execution_id="exec-1",
            result_id="result-1",
            lineage_level=LineageLevel.SOURCE,
            covered_operators=("other-output",),
            edge_digest="sha256:abc",
        ),
    )

    assert result.satisfied is False
    assert result.diagnostics[0].code == ReasonCode.LINEAGE_TARGET_NOT_COVERED


def test_empty_mask_cannot_be_structurally_valid(
    accept_plan: dict[str, Any],
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Mask",
            "operator_id": "empty-mask",
            "inputs": ["op1"],
            "fields": [],
        },
    ]
    raw["output_operator"] = "empty-mask"

    with pytest.raises(ValidationError):
        CandidatePlan.model_validate(raw)


def test_validator_rejects_rewriter_that_did_not_enforce_obligation(
    monkeypatch: pytest.MonkeyPatch,
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """Defense in depth: a future buggy rewrite cannot self-certify."""

    rule = policy_set.rules[0].model_copy(
        update={
            "obligations": (
                Obligation(
                    obligation_type=ObligationType.MASK,
                    parameters={"fields": ["event_id"]},
                ),
            )
        }
    )
    policy = policy_set.model_copy(update={"rules": (rule, *policy_set.rules[1:])})

    def broken_rewrite(plan: CandidatePlan, _evaluation: object) -> service.RewriteOutcome:
        # Simulate a regression that reports a rewrite reason without actually
        # putting the required Mask operator on the final output path.
        return service.RewriteOutcome(
            plan.operators,
            plan.output_operator,
            (ReasonCode.MASK_REQUIRED,),
        )

    monkeypatch.setattr(service, "_rewrite_obligations", broken_rewrite)

    result = service.validate(copy.deepcopy(accept_plan), policy, catalog)

    assert result.status == ValidationStatus.REJECT
    assert result.validated_plan is None
    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_NOT_ENFORCED


def test_generated_governance_id_avoids_untrusted_candidate_collision(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    raw = copy.deepcopy(rewrite_plan)
    reserved_id = "gov-001-generalizelocation"
    raw["operators"][2]["operator_id"] = reserved_id
    raw["output_operator"] = reserved_id

    result = service.validate(raw, policy_set, catalog)

    assert result.status == ValidationStatus.REWRITE
    assert result.validated_plan is not None
    ids = [operator.operator_id for operator in result.validated_plan.operators]
    assert len(ids) == len(set(ids))
    assert "gov-001-generalizelocation-1" in ids


def test_logical_lineage_rewrite_is_pending_until_evidence(
    rewrite_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    result = service.validate(copy.deepcopy(rewrite_plan), policy_set, catalog)

    assert result.status == ValidationStatus.REWRITE
    assert result.validated_plan is not None
    assert ObligationType.LINEAGE_CAPTURE not in result.validated_plan.satisfied_obligations
    assert ObligationType.LINEAGE_CAPTURE in result.validated_plan.pending_obligations
    assert result.validated_plan.lineage_requirements == (
        LineageRequirement(
            level=LineageLevel.RECORD,
            target_operator="op3",
        ),
    )
    assert result.validated_plan.lineage_instrumentation[0].level == LineageLevel.RECORD


def test_min_group_size_does_not_rewrite_detail_output(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """A k-anonymity guard cannot invent grouping for a row-level query."""

    rule = policy_set.rules[0].model_copy(
        update={
            "obligations": (
                Obligation(
                    obligation_type=ObligationType.MIN_GROUP_SIZE,
                    parameters={"minimum_count": 5},
                ),
            )
        }
    )
    policy = policy_set.model_copy(update={"rules": (rule, *policy_set.rules[1:])})

    result = service.validate(copy.deepcopy(accept_plan), policy, catalog)

    assert result.status == ValidationStatus.REJECT
    assert result.validated_plan is None
    assert result.diagnostics[0].code == ReasonCode.OBLIGATION_CONFLICT


def test_min_group_size_can_guard_existing_aggregate_output(
    accept_plan: dict[str, Any],
    policy_set: PolicySet,
    catalog: InMemoryCatalog,
) -> None:
    """When grouping is already explicit, the rewrite may append the guard."""

    raw = copy.deepcopy(accept_plan)
    raw["request_context"]["action"] = "aggregate"
    raw["operators"] = [
        raw["operators"][0],
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate",
            "inputs": ["op1"],
            "group_by": ["event_time"],
            "aggregates": [{"function": "count", "output_field": "event_count"}],
        },
    ]
    raw["output_operator"] = "op-aggregate"
    raw["requested_output"]["fields"] = ["event_time", "event_count"]
    rule = policy_set.rules[0].model_copy(
        update={
            "obligations": (
                Obligation(
                    obligation_type=ObligationType.MIN_GROUP_SIZE,
                    parameters={"minimum_count": 5},
                ),
            )
        }
    )
    policy = policy_set.model_copy(update={"rules": (rule, *policy_set.rules[1:])})

    result = service.validate(raw, policy, catalog)

    assert result.status == ValidationStatus.REWRITE
    assert result.validated_plan is not None
    assert any(isinstance(operator, Aggregate) for operator in result.validated_plan.operators)
    guards = [
        operator
        for operator in result.validated_plan.operators
        if isinstance(operator, MinGroupSize)
    ]
    assert len(guards) == 1
    assert guards[0].minimum_count == 5
