"""Tests for grouped V3 fitting and nested model selection helpers."""

from __future__ import annotations

import math

from trustaero.experiments.interaction_optimizer import (
    fit_interaction_mask_cost_model,
    select_inner_hyperparameters,
)
from trustaero.experiments.pipeline_optimizer import PipelineMaskFamilyObservation
from trustaero.optimizer.mask import MaskPlacementFeatures


def _observations() -> list[PipelineMaskFamilyObservation]:
    output: list[PipelineMaskFamilyObservation] = []
    for rows in (50_000, 100_000, 150_000, 200_000):
        for width in (128, 256, 512, 1024):
            for match in (0.5, 1.0):
                features = MaskPlacementFeatures(rows, width, match)
                rows_log = math.log1p(rows / 100_000.0)
                width_log = math.log1p(width / 64.0)
                observed = 0.8 - 0.7 * match - 0.2 * rows_log + 0.05 * width_log**2
                output.append(
                    PipelineMaskFamilyObservation(
                        family_id=f"n{rows}-w{width}-m{match}",
                        source_run_ids=("fixture",),
                        source_commit_hashes=("abc",),
                        seed_count=3,
                        features=features,
                        median_early_latency_ms=math.exp(observed) * 100.0,
                        median_late_latency_ms=100.0,
                        observed_log_early_late_ratio=observed,
                        tie_threshold_fraction=0.03,
                    )
                )
    return output


def test_fit_recovers_finite_predictions_and_provenance() -> None:
    observations = _observations()
    model = fit_interaction_mask_cost_model(observations, ridge_lambda=0.1)

    assert model.training_family_count == len(observations)
    assert model.source_run_ids == ("fixture",)
    assert all(
        math.isfinite(model.predict_log_early_late_ratio(item.features)) for item in observations
    )


def test_inner_selection_returns_only_frozen_grid_values() -> None:
    observations = _observations()
    ridge, quantile, threshold, candidates = select_inner_hyperparameters(
        observations,
        ridge_grid=(0.01, 0.1),
        uncertainty_quantile_grid=(0.5, 0.9),
    )

    assert ridge in {0.01, 0.1}
    assert quantile in {0.5, 0.9}
    assert threshold >= 0.0
    assert len(candidates) == 4
