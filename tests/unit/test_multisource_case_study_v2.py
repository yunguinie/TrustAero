"""Focused checks for the complete four-source V2 semantic loop."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.experiments.multisource_case_study_v2 import (
    _agent_plans,
    _candidate_profiles,
)
from trustaero.ir.enums import ReasonCode, ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.hierarchical_planner import (
    HierarchicalPlannerConfig,
    plan_governed_candidates,
)
from trustaero.planner.candidates import generate_duckdb_candidates
from trustaero.validator.service import validate

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "experiments/frozen/multisource_case_study_v2_protocol_20260726.json"


def _load(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _validated_fixture() -> tuple[object, tuple[object, ...]]:
    requests = _load(ROOT / "examples/multisource/agent_requests_v2.json")
    safe, _illegal = _agent_plans(ROOT, requests)
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load(ROOT / "examples/multisource/catalog.json"))
    )
    policy = PolicySet.model_validate(_load(ROOT / "examples/multisource/policy.json"))
    response = validate(safe, policy, catalog)
    assert response.validated_plan is not None
    candidates = generate_duckdb_candidates(
        response.validated_plan,
        materialization_targets=(
            "earthquake-magnitude",
            "earthquake-well-join",
            "gov-001-mask",
        ),
    )
    return response.validated_plan, candidates


def test_safe_and_illegal_agent_requests_are_distinct() -> None:
    requests = _load(ROOT / "examples/multisource/agent_requests_v2.json")
    safe, illegal = _agent_plans(ROOT, requests)
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load(ROOT / "examples/multisource/catalog.json"))
    )
    policy = PolicySet.model_validate(_load(ROOT / "examples/multisource/policy.json"))

    safe_response = validate(safe, policy, catalog)
    illegal_response = validate(illegal, policy, catalog)

    assert safe_response.status == ValidationStatus.REWRITE
    assert safe_response.validated_plan is not None
    assert {item.code for item in safe_response.diagnostics} == {
        ReasonCode.MASK_REQUIRED,
        ReasonCode.LINEAGE_REQUIRED,
    }
    assert illegal_response.status == ValidationStatus.REJECT
    assert illegal_response.validated_plan is None
    assert {item.code for item in illegal_response.diagnostics} == {
        ReasonCode.POLICY_NOT_APPLICABLE
    }


def test_physical_policy_prunes_before_optimizer_selection() -> None:
    _plan, candidates = _validated_fixture()
    artifacts = {
        "outputs": [
            {"artifact_id": "multisource_earthquakes_v1", "row_count": 992},
            {"artifact_id": "multisource_wells_v1", "row_count": 46350},
            {"artifact_id": "multisource_airports_v1", "row_count": 534},
            {"artifact_id": "multisource_cities_v1", "row_count": 1293},
        ]
    }
    profiles = _candidate_profiles(candidates, artifacts)
    selected = "materialize-after-gov-001-mask"
    decision = plan_governed_candidates(
        profiles,
        GovernanceFeasibilityPolicy(
            policy_id="test-no-raw",
            max_raw_join_rows=None,
            max_raw_materialized_rows=0,
            require_governance_checkpoint=True,
        ),
        HierarchicalPlannerConfig(conservative_fallback_candidate_id=selected),
    )

    assert decision.status == "SELECT"
    assert decision.selected_candidate_id == selected
    assert decision.feasible_candidate_ids == (selected,)
    assert set(decision.rejected_candidate_ids) == {
        "fused",
        "materialize-after-earthquake-magnitude",
        "materialize-after-earthquake-well-join",
    }
    assert decision.performance_model_used is False


def test_protocol_keeps_v4_and_performance_claims_out_of_scope() -> None:
    protocol = _load(PROTOCOL)
    assert protocol["experiment_role"] == "end_to_end_semantic_case_study_not_performance"
    lineage = protocol["lineage_scope"]
    assert isinstance(lineage, dict)
    assert lineage["level"] == "source"
    assert "one row-identity-preserving source" in lineage["boundary"]
    assert set(protocol["required_faults"]) == {
        "observed_result_digest_mismatch",
        "policy_snapshot_tamper",
        "data_snapshot_tamper",
        "lineage_coverage_removed",
        "physical_dependency_order_tamper",
        "planner_decision_tamper",
    }
