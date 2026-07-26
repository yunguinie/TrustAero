"""Tests for the policy-required checkpoint multi-factor gate."""

from __future__ import annotations

from trustaero.experiments.checkpoint_boundary_admission import (
    CheckpointBoundaryAdmissionConfig,
    analyze_checkpoint_boundary_admission,
)


def _config() -> CheckpointBoundaryAdmissionConfig:
    return CheckpointBoundaryAdmissionConfig(
        results_dir="results/test",
        measurement_results_dir="results/source",
        minimum_conclusive_scenario_rate=0.75,
        minimum_query_levels_with_cross_factor_winner_interaction=1,
        maximum_best_global_query_threshold_accuracy=0.9,
        require_policy_first_singleton_winner=True,
        require_query_first_singleton_winner=True,
        require_clean_git=True,
    )


def _scenario(width: int, policy: float, query: float, conclusion: str) -> dict:
    return {
        "scenario_id": f"n150000-w{width}-p{policy}-q{query}",
        "conclusion": conclusion,
    }


def test_cross_factor_interaction_authorizes_multifactor_development() -> None:
    summary = {
        "status": "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY",
        "scenario_results": [
            _scenario(128, 0.1, 0.1, "LEFT_MATERIALLY_FASTER"),
            _scenario(1024, 0.5, 0.1, "LEFT_MATERIALLY_SLOWER"),
            _scenario(128, 0.1, 0.13, "LEFT_MATERIALLY_FASTER"),
            _scenario(1024, 0.5, 0.13, "LEFT_MATERIALLY_SLOWER"),
        ],
    }
    result = analyze_checkpoint_boundary_admission(summary, _config())

    assert result["status"] == "PASS_CHECKPOINT_MULTIFACTOR_OPTIMIZER_ADMISSION"
    assert result["query_levels_with_cross_factor_winner_interaction"] == [0.1, 0.13]
    assert result["best_global_query_threshold_accuracy"] == 0.5


def test_global_query_threshold_blocks_unnecessary_model() -> None:
    summary = {
        "status": "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY",
        "scenario_results": [
            _scenario(128, 0.1, 0.07, "LEFT_MATERIALLY_SLOWER"),
            _scenario(1024, 0.5, 0.07, "LEFT_MATERIALLY_SLOWER"),
            _scenario(128, 0.1, 0.16, "LEFT_MATERIALLY_FASTER"),
            _scenario(1024, 0.5, 0.16, "LEFT_MATERIALLY_FASTER"),
        ],
    }
    result = analyze_checkpoint_boundary_admission(summary, _config())

    assert result["status"] == "FAIL_CHECKPOINT_MULTIFACTOR_OPTIMIZER_ADMISSION_RETAIN"
    assert result["best_global_query_threshold_accuracy"] == 1.0
    assert not result["gate_checks"]["global_threshold_not_sufficient"]
