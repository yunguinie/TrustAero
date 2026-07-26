"""Tests for the model-free optimizer workload-sufficiency audit."""

from __future__ import annotations

from trustaero.experiments.optimizer_workload_sufficiency import (
    WorkloadSufficiencyConfig,
    WorkloadSufficiencyGates,
    audit_workload_sufficiency,
)


def _family(
    family_id: str,
    *,
    rate: float,
    direction: str,
    width: int,
    group: str,
) -> dict[str, object]:
    return {
        "family_id": family_id,
        "stable_for_model_evaluation": True,
        "target_match_rate": rate,
        "identifier_width_bytes": width,
        "scenario_group": group,
        "directions": {"overall": direction},
    }


def _config(template_count: int = 2) -> WorkloadSufficiencyConfig:
    return WorkloadSufficiencyConfig(
        protocol_name="test",
        results_dir="results/test",
        paired_stability_audit_path="paired.json",
        paired_stability_audit_sha256="a" * 64,
        v41_result_path="v41.json",
        v41_result_sha256="b" * 64,
        query_template_ids=tuple(f"q{index}" for index in range(template_count)),
        require_clean_git=False,
        gates=WorkloadSufficiencyGates(
            minimum_stable_family_count=2,
            minimum_query_template_count=2,
            minimum_fixed_match_rate_reversal_strata=1,
            maximum_match_rate_baseline_top1=0.95,
            minimum_identifier_width_levels=2,
            minimum_complete_time_groups=2,
        ),
        scientific_boundary="test only",
    )


def _v41(top1: float) -> dict[str, object]:
    return {
        "external_partition_accessed": False,
        "deployed_metrics": {
            "match_rate_baseline": {
                "top1_selection_rate": top1,
                "mean_regret_percent": 1.0,
            }
        },
    }


def test_audit_rejects_match_rate_separable_fragment() -> None:
    paired = {
        "status": "PASS_PAIRED_STABILITY_AUDIT",
        "external_partition_accessed": False,
        "family_audits": [
            _family("a", rate=0.25, direction="late_mask", width=192, group="g1"),
            _family(
                "b",
                rate=0.70,
                direction="early_mask_materialized",
                width=384,
                group="g2",
            ),
        ],
    }
    result = audit_workload_sufficiency(paired, _v41(1.0), _config())
    assert result["status"] == "FAIL_WORKLOAD_DISCRIMINATIVE_SUFFICIENCY"
    assert result["fixed_match_rate_reversal_strata"] == 0
    assert result["pipeline_model_authorized"] is False


def test_audit_accepts_same_match_reversal_with_nontrivial_baseline() -> None:
    paired = {
        "status": "PASS_PAIRED_STABILITY_AUDIT",
        "external_partition_accessed": False,
        "family_audits": [
            _family("a", rate=0.50, direction="late_mask", width=192, group="g1"),
            _family(
                "b",
                rate=0.50,
                direction="early_mask_materialized",
                width=384,
                group="g2",
            ),
        ],
    }
    result = audit_workload_sufficiency(paired, _v41(0.5), _config())
    assert result["status"] == "PASS_WORKLOAD_DISCRIMINATIVE_SUFFICIENCY"
    assert result["fixed_match_rate_reversal_strata"] == 1
    assert result["pipeline_model_authorized"] is True
