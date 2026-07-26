"""Tests for the model-free multi-candidate admission gate."""

from __future__ import annotations

from copy import deepcopy

from trustaero.experiments.multicandidate_admission import (
    AdmissionGates,
    audit_multicandidate_admission,
)


def _gates() -> AdmissionGates:
    return AdmissionGates(3, 4, 2, 1, True, True, True)


def _sources() -> tuple[dict, ...]:
    query_protocol = {
        "templates": [
            {
                "template_id": "QF-BTS-MASKED-READ",
                "workload_id": "bts",
                "stage": "semantic_ready",
                "governance_profiles": [
                    {
                        "expected_feasible_candidates": ["fused", "after-mask"],
                        "expected_rejected_candidates": ["after-filter"],
                    },
                    {
                        "expected_feasible_candidates": [
                            "fused",
                            "after-filter",
                            "after-mask",
                        ],
                        "expected_rejected_candidates": [],
                    },
                ],
            },
            {
                "template_id": "QF-NYC-ZONE-AGGREGATE",
                "workload_id": "nyc",
                "stage": "semantic_ready",
                "governance_profiles": [
                    {
                        "expected_feasible_candidates": ["fused", "filter", "join"],
                        "expected_rejected_candidates": [],
                    }
                ],
            },
            {
                "template_id": "QF-BTS-NATURAL-MULTIJOIN",
                "workload_id": "multi",
                "stage": "semantic_ready",
                "governance_profiles": [
                    {
                        "expected_feasible_candidates": ["fused", "join-a", "join-b"],
                        "expected_rejected_candidates": [],
                    }
                ],
            },
        ]
    }
    real = {
        "status": "PASS",
        "observations": [
            {
                "unit_id": "bts-full-2024-01",
                "feasible_candidate_ids": ["fused", "after-filter", "after-mask"],
                "oracle_set_within_3_percent": ["after-mask"],
            },
            {
                "unit_id": "nyc_tlc-full-2024-01",
                "feasible_candidate_ids": ["fused", "filter", "join"],
                "oracle_set_within_3_percent": ["fused"],
            },
        ],
    }
    multijoin = {
        "status": "PASS",
        "diagnostic_oracle_set_within_tie_band": ["fused"],
        "median_candidate_over_fused_ratio": {
            "fused": 1.0,
            "join-a": 1.2,
            "join-b": 1.3,
        },
    }
    tpch = {
        "status": "PASS",
        "diagnostic_oracle_set_within_tie_band": ["fused"],
        "median_candidate_over_fused_ratio": {
            "fused": 1.0,
            "predicate": 1.1,
            "time": 5.0,
        },
    }
    development = {
        "policy_first_winner_count": 4,
        "query_first_winner_count": 3,
        "reversal_discovery": "STABLE_BIDIRECTIONAL_REVERSAL_DISCOVERED",
    }
    real_reversal = {
        "policy_first_winner_count": 19,
        "query_first_winner_count": 26,
        "reversal_discovery": "STABLE_BIDIRECTIONAL_REVERSAL_DISCOVERED",
    }
    return query_protocol, real, multijoin, tpch, development, real_reversal


def test_current_shape_rejects_cross_family_memorization() -> None:
    result = audit_multicandidate_admission(*_sources(), _gates())
    assert result["status"] == "FAIL_MULTICANDIDATE_OPTIMIZER_ADMISSION_RETAIN"
    assert result["global_winner_classes"] == ["BASELINE", "NONBASELINE"]
    assert result["three_candidate_families_with_internal_winner_diversity"] == []
    assert result["gate_checks"]["governance_candidate_pruning"] is True
    assert not result["gate_checks"][
        "minimum_three_candidate_families_with_internal_winner_diversity"
    ]


def test_internal_three_candidate_reversal_authorizes_training() -> None:
    sources = list(deepcopy(_sources()))
    sources[1]["observations"].append(
        {
            "unit_id": "bts-full-2024-01",
            "feasible_candidate_ids": ["fused", "after-filter", "after-mask"],
            "oracle_set_within_3_percent": ["fused"],
        }
    )
    result = audit_multicandidate_admission(*sources, _gates())
    assert result["status"] == "PASS_MULTICANDIDATE_OPTIMIZER_ADMISSION"
    assert result["optimizer_training_authorized"] is True
    assert result["three_candidate_families_with_internal_winner_diversity"] == [
        "QF-BTS-MASKED-READ"
    ]


def test_real_reversal_is_a_hard_gate() -> None:
    sources = list(deepcopy(_sources()))
    sources[5]["query_first_winner_count"] = 0
    result = audit_multicandidate_admission(*sources, _gates())
    assert result["optimizer_training_authorized"] is False
    assert result["gate_checks"]["real_bidirectional_reversal"] is False
