"""Tests for shape isolation, uncertainty fallback, and grouped validation."""

from __future__ import annotations

from trustaero.experiments.execution_aware_calibration import CalibrationObservation
from trustaero.experiments.execution_aware_shape_calibration import (
    SHAPE_STABLE_PREFERENCES,
    _candidate_support,
    _evaluate_group,
    _fit_shape_ensemble,
    grouped_shape_validation,
)


def _observation(
    scenario: str,
    seed: int,
    group: str,
    candidate: str,
    work: float,
    latency: float,
) -> CalibrationObservation:
    return CalibrationObservation(
        scenario_id=scenario,
        seed=seed,
        equivalence_group=group,
        candidate_id=candidate,
        latency_ms=latency,
        features=(("operator.work", work),),
    )


def test_shape_ensemble_can_override_fallback_only_by_consensus() -> None:
    training = tuple(
        _observation(f"train-{index}", 1, "output", candidate, work, latency)
        for index in range(6)
        for candidate, work, latency in (
            ("fallback", 2.0, 20.0),
            ("faster", 1.0, 10.0),
        )
    )
    fits = _fit_shape_ensemble(training, (1e-6, 1e-4, 1e-2))
    testing = (
        _observation("held", 7, "output", "fallback", 2.0, 20.0),
        _observation("held", 7, "output", "faster", 1.0, 10.0),
    )

    decision = _evaluate_group(
        fits,
        _candidate_support(training),
        testing,
        stable_preference="fallback",
        practical_tie_fraction=0.03,
    )[0]

    assert decision["selected_candidate_id"] == "faster"
    assert decision["fallback_reason"] is None
    assert decision["oracle_hit"] is True


def test_shape_ensemble_fails_closed_outside_candidate_support() -> None:
    training = (
        _observation("train", 1, "output", "fallback", 1.0, 10.0),
        _observation("train", 1, "output", "faster", 1.0, 9.0),
    )
    fits = _fit_shape_ensemble(training, (1e-6,))
    testing = (
        _observation("held", 7, "output", "fallback", 5.0, 10.0),
        _observation("held", 7, "output", "faster", 5.0, 9.0),
    )

    decision = _evaluate_group(
        fits,
        _candidate_support(training),
        testing,
        stable_preference="fallback",
        practical_tie_fraction=0.03,
    )[0]

    assert decision["selected_candidate_id"] == "fallback"
    assert decision["fallback_reason"] == "out_of_support"


def test_shape_validation_keeps_complete_scenario_families() -> None:
    observations: list[CalibrationObservation] = []
    for scenario in range(4):
        for seed in (1, 2, 3):
            for group, fallback in SHAPE_STABLE_PREFERENCES.items():
                observations.extend(
                    (
                        _observation(f"scenario-{scenario}", seed, group, fallback, 1.0, 10.0),
                        _observation(
                            f"scenario-{scenario}",
                            seed,
                            group,
                            f"alternative-{group}",
                            2.0,
                            20.0,
                        ),
                    )
                )

    result = grouped_shape_validation(tuple(observations), lambda_grid=(1e-6,))

    assert result["outer_fold_count"] == 4
    assert result["decision_count"] == 48
    assert result["oracle_set_hit_rate"] == 1.0
    assert result["maximum_regret_percent"] == 0.0
    assert all(item["training_scenario_count"] == 3 for item in result["folds"])
