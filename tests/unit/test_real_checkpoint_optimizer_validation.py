from __future__ import annotations

from trustaero.experiments.real_checkpoint_optimizer_validation import (
    RealCheckpointValidationConfig,
    threshold_candidate,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)


def _statistics(query_rows: int) -> GovernedCheckpointStatistics:
    return GovernedCheckpointStatistics(
        input_rows=1000,
        sensitive_width_bytes=256.0,
        estimated_policy_rows=300,
        estimated_query_rows=query_rows,
        estimated_result_rows=min(300, query_rows),
        statistic_provenance="catalog_exact_controlled",
    )


def test_frozen_threshold_has_deterministic_boundary_behavior() -> None:
    assert threshold_candidate(_statistics(349), 0.35) == QUERY_FIRST_CHECKPOINT
    assert threshold_candidate(_statistics(350), 0.35) == POLICY_FIRST_CHECKPOINT


def test_v4_defaults_preserve_frozen_tie_behavior() -> None:
    config = RealCheckpointValidationConfig(
        results_dir="results/test",
        model_path="model.json",
        calibration_record_path="calibration.json",
        measurement_config_path="measurement.json",
        expected_model_sha256="0" * 64,
        expected_calibration_sha256="1" * 64,
        expected_measurement_config_sha256="2" * 64,
        frozen_query_selectivity_threshold=0.35,
        minimum_analytic_confidence_family_hit_rate=0.85,
        maximum_analytic_mean_regret_percent=3.0,
        maximum_analytic_p95_regret_percent=10.0,
        maximum_analytic_regret_percent=25.0,
        require_analytic_family_hit_no_worse_than_threshold=True,
        require_analytic_mean_regret_no_worse_than_threshold=True,
        require_seed_consistency=True,
        maximum_out_of_support_fallback_rate=0.0,
        require_clean_git=True,
    )

    assert config.optimizer_version == "V4"
    assert config.practical_tie_strategy == "policy_first_fallback"
    assert config.authorize_final_holdout_claim is False
