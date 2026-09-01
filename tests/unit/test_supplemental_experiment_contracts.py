"""Regression tests for supplemental cross-stage and process-separated experiments."""

from __future__ import annotations

from scripts.run_cross_stage_contract_ablation import _profile
from scripts.run_independent_checker_manifest_experiment import _case_inputs


def test_cross_stage_profile_requires_all_three_guarantees() -> None:
    validator = {
        "trustaero_full": {"unsafe_acceptance_count": 0, "false_reject_count": 0},
        "policy_output_only": {"unsafe_acceptance_count": 0, "false_reject_count": 1},
    }
    planner = {
        "permissive": {
            "legality_first_illegal_selection_count": 0,
            "governance_blind_illegal_selection_count": 0,
        },
        "strict": {
            "legality_first_illegal_selection_count": 0,
            "governance_blind_illegal_selection_count": 96,
        },
    }
    certificate = {
        "trustaero_certificate_full": {"detected_fault_count": 19, "fault_count": 19},
        "ordinary_event_log": {"detected_fault_count": 6, "fault_count": 19},
    }
    full = _profile(
        "full",
        logical_approval=True,
        legality_first=True,
        evidence_bound=True,
        validator=validator,
        planner=planner,
        certificate=certificate,
    )
    ablated = _profile(
        "ablated",
        logical_approval=True,
        legality_first=True,
        evidence_bound=False,
        validator=validator,
        planner=planner,
        certificate=certificate,
    )
    assert full["cross_stage_contract_complete"] is True
    assert ablated["cross_stage_contract_complete"] is False


def test_manifest_cases_mutate_only_registered_input() -> None:
    logical = {"logical_plan_id": "pl-ok"}
    physical = {"logical_plan_id": "pl-ok"}
    certificate = {"result_digest": "sha256:result"}
    observation = {"result_digest": "sha256:result"}

    baseline = _case_inputs("untampered", logical, physical, certificate, observation)
    observed = _case_inputs("observed_result_tamper", logical, physical, certificate, observation)
    certified = _case_inputs(
        "certificate_result_tamper", logical, physical, certificate, observation
    )
    manifest = _case_inputs("approval_manifest_tamper", logical, physical, certificate, observation)

    assert baseline == (logical, physical, certificate, observation)
    assert observed[3]["result_digest"] != observation["result_digest"]
    assert certified[2]["result_digest"] != certificate["result_digest"]
    assert manifest[1]["logical_plan_id"] != physical["logical_plan_id"]
    assert logical == {"logical_plan_id": "pl-ok"}
    assert physical == {"logical_plan_id": "pl-ok"}
