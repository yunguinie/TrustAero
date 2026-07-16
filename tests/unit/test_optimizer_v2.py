"""Tests for V2 fitting and leakage-resistant grouped cross-validation."""

from __future__ import annotations

import math

from trustaero.experiments.optimizer_v2 import (
    MaskWorkloadObservation,
    audit_match_rate_monotonicity,
    cross_validate_mask_v2,
    fit_mask_v2_model,
)
from trustaero.optimizer.mask import MaskPlacementFeatures
from trustaero.optimizer.mask_v2 import MaskV2Model, mask_v2_feature_vector


def _observations() -> list[MaskWorkloadObservation]:
    output: list[MaskWorkloadObservation] = []
    for scenario_index, width in enumerate((128, 512, 1024, 2048)):
        for row_count in (100_000, 300_000):
            features = MaskPlacementFeatures(
                join_input_rows=row_count,
                identifier_width_bytes=width,
                join_match_rate=(scenario_index + 1) / 4,
            )
            vector = mask_v2_feature_vector(features)
            # The deterministic linear target tests fitting without embedding
            # a database timing assumption in the unit test.
            target = 0.15 - 0.03 * vector[0] - 0.04 * vector[3] + 0.1 * vector[2]
            output.append(
                MaskWorkloadObservation(
                    workload_id=f"run/s{scenario_index}/n{row_count}",
                    scenario_group_id=f"run/s{scenario_index}",
                    source_run_id="run",
                    source_commit_hash="abc123",
                    scenario_id=f"s{scenario_index}",
                    row_count=row_count,
                    seed_count=3,
                    features=features,
                    observed_log_early_late_ratio=target,
                    median_early_latency_ms=100.0 * math.exp(target),
                    median_late_latency_ms=100.0,
                    tie_threshold_fraction=0.03,
                )
            )
    return output


def test_fit_produces_finite_serializable_model() -> None:
    observations = _observations()
    model = fit_mask_v2_model(observations)

    prediction = model.predict_log_latency_ratio(observations[0].features)
    assert math.isfinite(prediction)
    assert model.to_dict()["training_sample_count"] == 8


def test_scenario_cross_validation_keeps_each_family_in_one_fold() -> None:
    rows = cross_validate_mask_v2(_observations(), split="scenario")

    assert len(rows) == 8
    for row in rows:
        assert row["holdout_group"] == row["scenario_group_id"]
        assert row["evaluation_scheme"] == "v2_leave_one_scenario_out"


def test_match_rate_monotonicity_audit_detects_wrong_direction() -> None:
    model = MaskV2Model(
        intercept=0.0,
        coefficients=(0.0, 0.0, 1.0, 0.0, 0.0),
        feature_means=(0.0, 0.0, 0.0, 0.0, 0.0),
        feature_scales=(1.0, 1.0, 1.0, 1.0, 1.0),
        ridge_lambda=0.01,
        training_sample_count=10,
    )

    audit = audit_match_rate_monotonicity(
        model,
        row_counts=(100_000,),
        identifier_widths=(512,),
        match_rates=(0.1, 0.5, 1.0),
    )

    assert audit["comparison_count"] == 2
    assert audit["violation_count"] == 2
    assert audit["passes"] is False
