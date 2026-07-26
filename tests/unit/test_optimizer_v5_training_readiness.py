from __future__ import annotations

from trustaero.experiments.optimizer_v5_training_readiness import (
    TrainingReadinessConfig,
    TrainingReadinessGates,
    _label_class,
    audit_optimizer_v5_training_readiness,
)


def _config() -> TrainingReadinessConfig:
    return TrainingReadinessConfig(
        protocol_name="test",
        results_dir="results/test",
        pairwise_inference_path="inference.json",
        pairwise_inference_sha256="a" * 64,
        pairwise_summary_path="summary.json",
        pairwise_summary_sha256="b" * 64,
        multijoin_acceptance_path="multijoin.json",
        multijoin_acceptance_sha256="c" * 64,
        mask_join_acceptance_path="mask.json",
        mask_join_acceptance_sha256="d" * 64,
        query_family_protocol_path="queries.json",
        query_family_protocol_sha256="e" * 64,
        v5_unit_template_ids={
            "nyc_tlc-n100000": "QF-NYC",
            "nyc_tlc-n500000": "QF-NYC",
        },
        multijoin_template_id="QF-MULTIJOIN",
        mask_join_template_id="QF-MASK-JOIN",
        require_clean_git=True,
        gates=TrainingReadinessGates(
            minimum_performance_unit_count=4,
            minimum_workload_family_count=3,
            minimum_query_template_count=3,
            minimum_multi_candidate_unit_count=4,
            minimum_distinct_label_class_count=2,
            minimum_baseline_winner_count=1,
            minimum_unrestricted_nonbaseline_winner_count=1,
            minimum_governance_forced_nonbaseline_count=1,
            maximum_dominant_label_fraction=0.8,
        ),
        scientific_boundary="development only",
    )


def _sources(nonbaseline_v5_winner: bool = False) -> tuple[dict, ...]:
    first_label = ["materialize-after-nyc-zone-join"] if nonbaseline_v5_winner else ["fused"]
    inference = {
        "status": "PASS_V5_PAIRWISE_LABEL_GATE",
        "external_partition_accessed": False,
        "profile_labels": [
            {
                "unit_id": "nyc_tlc-n100000",
                "policy_profile": "source-lineage",
                "feasible_candidate_ids": [
                    "fused",
                    "materialize-after-nyc-zone-join",
                ],
                "authorized_oracle_set": first_label,
                "model_label_authorized": True,
            },
            {
                "unit_id": "nyc_tlc-n500000",
                "policy_profile": "source-lineage",
                "feasible_candidate_ids": [
                    "fused",
                    "materialize-after-nyc-zone-join",
                ],
                "authorized_oracle_set": ["fused"],
                "model_label_authorized": True,
            },
        ],
    }
    summary = {"external_partition_accessed": False}
    multijoin = {
        "status": "PASS",
        "formal_paper_experiment_authorized": True,
        "heldout_optimizer_evidence": False,
        "diagnostic_oracle_set_within_tie_band": ["fused"],
        "median_candidate_over_fused_ratio": {
            "fused": 1.0,
            "materialize-after-filter": 1.2,
        },
    }
    mask_join = {
        "status": "PASS",
        "formal_paper_experiment_authorized": True,
        "heldout_optimizer_evidence": False,
        "diagnostic_oracle_set_within_tie_band": [
            "early_mask_before_join",
            "late_mask_fused",
        ],
        "strict_policy_feasible_set": ["early_mask_before_join"],
    }
    queries = {
        "templates": [
            {"template_id": "QF-NYC", "stage": "semantic_ready"},
            {"template_id": "QF-MULTIJOIN", "stage": "semantic_ready"},
            {"template_id": "QF-MASK-JOIN", "stage": "semantic_ready"},
        ]
    }
    return inference, summary, multijoin, mask_join, queries


def test_label_class_distinguishes_cost_choice_from_tie() -> None:
    assert _label_class(("fused",), "fused") == "BASELINE_ONLY"
    assert _label_class(("materialized",), "fused") == "NONBASELINE_SINGLETON"
    assert _label_class(("fused", "materialized"), "fused") == "PRACTICAL_TIE_INCLUDING_BASELINE"


def test_readiness_rejects_only_baseline_and_tie_evidence() -> None:
    result = audit_optimizer_v5_training_readiness(*_sources(), _config())
    assert result["status"] == "FAIL_OPTIMIZER_V5_TRAINING_READINESS"
    assert result["performance_unit_count"] == 4
    assert result["workload_families"] == [
        "bts_mask_join",
        "bts_multijoin",
        "nyc_tlc",
    ]
    assert result["unrestricted_nonbaseline_winner_count"] == 0
    assert result["governance_forced_nonbaseline_count"] == 1
    assert not result["gate_checks"]["minimum_unrestricted_nonbaseline_winner_count"]


def test_readiness_accepts_a_measured_nonbaseline_winner() -> None:
    result = audit_optimizer_v5_training_readiness(*_sources(nonbaseline_v5_winner=True), _config())
    assert result["status"] == "PASS_OPTIMIZER_V5_TRAINING_READINESS"
    assert result["optimizer_v5_training_authorized"] is True
    assert result["unrestricted_nonbaseline_winner_count"] == 1
