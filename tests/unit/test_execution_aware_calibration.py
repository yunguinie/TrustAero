"""Tests for nonnegative fitting and leakage-safe grouped evaluation."""

from __future__ import annotations

import pytest

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
    evaluate_fit_selections,
    fit_nonnegative_analytic_cost,
    grouped_outer_validation,
)


def _observation(
    scenario: str,
    seed: int,
    candidate: str,
    work: float,
    latency: float,
) -> CalibrationObservation:
    return CalibrationObservation(
        scenario_id=scenario,
        seed=seed,
        equivalence_group="output",
        candidate_id=candidate,
        latency_ms=latency,
        features=(("work.rows_million", work),),
    )


def test_nonnegative_fit_recovers_additive_cost() -> None:
    observations = tuple(
        _observation(f"scenario-{index}", 1, "candidate", float(index), 2.0 + 3.0 * index)
        for index in range(1, 10)
    )

    fit = fit_nonnegative_analytic_cost(observations, ridge_lambda=1e-6)

    assert fit.converged is True
    assert fit.intercept_ms == pytest.approx(2.0, abs=1e-3)
    assert dict(fit.coefficients)["work.rows_million"] == pytest.approx(3.0, abs=1e-3)


def test_selection_uses_stable_preference_only_inside_predicted_tie() -> None:
    fit = fit_nonnegative_analytic_cost(
        tuple(
            _observation(f"train-{index}", 1, "train", index, 1 + index) for index in range(1, 8)
        ),
        ridge_lambda=1e-6,
    )
    test = (
        _observation("held", 7, "preferred", 1.01, 10.1),
        _observation("held", 7, "other", 1.0, 10.0),
    )

    result = evaluate_fit_selections(
        fit,
        test,
        stable_preferences={"output": "preferred"},
        practical_tie_fraction=0.03,
    )[0]

    assert result.selected_candidate_id == "preferred"
    assert result.oracle_candidate_ids == ("other", "preferred")
    assert result.oracle_hit is True


def test_outer_validation_holds_out_all_seeds_of_one_scenario() -> None:
    observations = tuple(
        _observation(
            f"scenario-{scenario}",
            seed,
            candidate,
            1.0 if candidate == "fast" else 2.0,
            10.0 if candidate == "fast" else 20.0,
        )
        for scenario in range(6)
        for seed in (1, 2, 3)
        for candidate in ("fast", "slow")
    )

    result = grouped_outer_validation(
        observations,
        lambda_grid=(1e-6, 1e-4),
        stable_preferences={"output": "fast"},
    )

    assert result["outer_fold_count"] == 6
    assert result["decision_count"] == 18
    assert result["oracle_set_hit_rate"] == 1.0
    assert result["maximum_regret_percent"] == 0.0
    assert all(item["training_scenario_count"] == 5 for item in result["folds"])
