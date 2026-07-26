"""Tests for candidate-specific cost curves and conservative guards."""

from __future__ import annotations

from trustaero.experiments.execution_aware_calibration import CalibrationObservation
from trustaero.experiments.execution_aware_candidate_calibration import (
    DEPLOYABLE_STABLE_PREFERENCES,
    _evaluate_candidate_ensembles,
    _fit_candidate_ensembles,
)
from trustaero.experiments.execution_aware_shape_calibration import _candidate_support


def _observation(
    scenario: str,
    candidate: str,
    work: float,
    latency: float,
) -> CalibrationObservation:
    return CalibrationObservation(
        scenario_id=scenario,
        seed=1,
        equivalence_group="output",
        candidate_id=candidate,
        latency_ms=latency,
        features=(("pipeline.work", work),),
    )


def test_mechanism_only_group_is_not_deployable() -> None:
    assert "column_pruning" not in DEPLOYABLE_STABLE_PREFERENCES
    assert set(DEPLOYABLE_STABLE_PREFERENCES) == {
        "mask_output",
        "mask_aggregate",
        "mask_sorted_output",
    }


def test_candidate_specific_cost_curves_can_cross() -> None:
    # Candidate A has a higher fixed cost but lower marginal cost; candidate B
    # is faster for small work and slower for large work. Separate formulas are
    # required to represent this ordinary optimizer crossover.
    training = tuple(
        observation
        for work in (1.0, 2.0, 3.0, 4.0)
        for observation in (
            _observation(f"train-{work}", "candidate_a", work, 5.0 + work),
            _observation(f"train-{work}", "candidate_b", work, 1.0 + 3.0 * work),
        )
    )
    fits = _fit_candidate_ensembles(training, (1e-6, 1e-4))
    support = _candidate_support(training)

    small = _evaluate_candidate_ensembles(
        fits,
        support,
        (
            _observation("small", "candidate_a", 1.0, 6.0),
            _observation("small", "candidate_b", 1.0, 4.0),
        ),
        stable_preference="candidate_a",
        practical_tie_fraction=0.03,
    )[0]
    large = _evaluate_candidate_ensembles(
        fits,
        support,
        (
            _observation("large", "candidate_a", 4.0, 9.0),
            _observation("large", "candidate_b", 4.0, 13.0),
        ),
        stable_preference="candidate_a",
        practical_tie_fraction=0.03,
    )[0]

    assert small["selected_candidate_id"] == "candidate_b"
    assert large["selected_candidate_id"] == "candidate_a"
    assert small["oracle_hit"] is True
    assert large["oracle_hit"] is True


def test_candidate_specific_cost_fails_closed_outside_support() -> None:
    training = tuple(
        observation
        for work in (1.0, 2.0, 3.0)
        for observation in (
            _observation(f"train-{work}", "fallback", work, 5.0 + work),
            _observation(f"train-{work}", "alternative", work, 2.0 + work),
        )
    )
    fits = _fit_candidate_ensembles(training, (1e-6,))
    decision = _evaluate_candidate_ensembles(
        fits,
        _candidate_support(training),
        (
            _observation("held", "fallback", 10.0, 15.0),
            _observation("held", "alternative", 10.0, 12.0),
        ),
        stable_preference="fallback",
        practical_tie_fraction=0.03,
    )[0]

    assert decision["selected_candidate_id"] == "fallback"
    assert decision["guard_reason"] == "out_of_support"
    assert decision["view_choices"] == []
