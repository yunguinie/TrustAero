"""Query-shape-aware development calibration for Execution-Aware planning.

The first analytic model shared one coefficient vector across physically
different result shapes.  This module keeps the same auditable work features
and family-level holdout boundary, but fits one non-negative ensemble per
equivalence group.  It is deliberately conservative: a model may override the
stable legal fallback only when every regularization view agrees and all
candidate work vectors remain inside training support.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
    NonnegativeAnalyticFit,
    fit_nonnegative_analytic_cost,
    load_calibration_observations,
)
from trustaero.experiments.execution_flow_audit import _atomic_json

# These fallbacks are legal candidates for their result-equivalent groups.
# They were chosen from the completed development diagnosis and therefore must
# be evaluated later on a newly frozen matrix, not advertised as holdout wins.
SHAPE_STABLE_PREFERENCES = {
    "column_pruning": "join_key_only_aggregate",
    "mask_output": "postjoin_mask_fused_output",
    "mask_aggregate": "postjoin_raw_materialized_mask_aggregate",
    "mask_sorted_output": "postjoin_mask_fused_sorted_output",
}

# Shape-specific subsets contain more collinear components than the original
# pooled fit.  A relative objective change of 1e-6 is a standard numerical
# stopping scale for this development solver and avoids spending thousands of
# iterations on coefficient exchange that does not change candidate ranking.
SHAPE_MAXIMUM_ITERATIONS = 10_000
SHAPE_OBJECTIVE_TOLERANCE = 1e-6


def _candidate_support(
    observations: tuple[CalibrationObservation, ...],
) -> dict[str, dict[str, tuple[float, float]]]:
    """Return per-candidate feature bounds without using execution labels."""

    values: dict[str, dict[str, list[float]]] = {}
    for observation in observations:
        candidate = values.setdefault(observation.candidate_id, {})
        for name, value in observation.features:
            candidate.setdefault(name, []).append(value)
    return {
        candidate_id: {name: (min(samples), max(samples)) for name, samples in features.items()}
        for candidate_id, features in values.items()
    }


def _inside_support(
    observation: CalibrationObservation,
    support: dict[str, dict[str, tuple[float, float]]],
) -> bool:
    """Fail closed when any physical-work feature leaves calibration support."""

    candidate_support = support.get(observation.candidate_id)
    if candidate_support is None:
        return False
    values = dict(observation.features)
    if set(values) != set(candidate_support):
        return False
    for name, value in values.items():
        lower, upper = candidate_support[name]
        tolerance = 1e-12 * max(1.0, abs(lower), abs(upper))
        if value < lower - tolerance or value > upper + tolerance:
            return False
    return True


def _fit_shape_ensemble(
    observations: tuple[CalibrationObservation, ...],
    lambda_grid: tuple[float, ...],
) -> tuple[NonnegativeAnalyticFit, ...]:
    """Fit multiple analytic views; disagreement becomes uncertainty."""

    fits: list[NonnegativeAnalyticFit] = []
    for ridge_lambda in lambda_grid:
        fit = fit_nonnegative_analytic_cost(
            observations,
            ridge_lambda=ridge_lambda,
            maximum_iterations=SHAPE_MAXIMUM_ITERATIONS,
            tolerance=SHAPE_OBJECTIVE_TOLERANCE,
        )
        if not fit.converged:
            raise ValueError(
                "Shape-aware analytic view did not converge: "
                f"lambda={ridge_lambda}, iterations={fit.iterations}"
            )
        fits.append(fit)
    return tuple(fits)


def _fit_choice(
    fit: NonnegativeAnalyticFit,
    candidates: tuple[CalibrationObservation, ...],
    *,
    stable_preference: str,
    practical_tie_fraction: float,
) -> str:
    """Choose the predicted best candidate, preferring fallback within a tie."""

    predicted = {item.candidate_id: fit.predict_ms(item.features) for item in candidates}
    predicted_best = min(predicted.values())
    practical_set = {
        candidate_id
        for candidate_id, latency in predicted.items()
        if latency <= predicted_best * (1.0 + practical_tie_fraction)
    }
    if stable_preference in practical_set:
        return stable_preference
    return min(practical_set, key=lambda item: (predicted[item], item))


def _evaluate_group(
    fits: tuple[NonnegativeAnalyticFit, ...],
    support: dict[str, dict[str, tuple[float, float]]],
    observations: tuple[CalibrationObservation, ...],
    *,
    stable_preference: str,
    practical_tie_fraction: float,
) -> list[dict[str, Any]]:
    """Rank legal candidates, recording every conservative fallback reason."""

    by_seed: dict[tuple[str, int], list[CalibrationObservation]] = {}
    for observation in observations:
        by_seed.setdefault((observation.scenario_id, observation.seed), []).append(observation)
    decisions: list[dict[str, Any]] = []
    for (scenario_id, seed), raw_candidates in sorted(by_seed.items()):
        candidates = tuple(raw_candidates)
        candidate_ids = {item.candidate_id for item in candidates}
        fallback_reason: str | None
        ensemble_choices: tuple[str, ...]
        if stable_preference not in candidate_ids:
            raise ValueError("Stable shape fallback is absent from candidate set")
        if not all(_inside_support(item, support) for item in candidates):
            selected = stable_preference
            fallback_reason = "out_of_support"
            ensemble_choices = ()
        else:
            ensemble_choices = tuple(
                _fit_choice(
                    fit,
                    candidates,
                    stable_preference=stable_preference,
                    practical_tie_fraction=practical_tie_fraction,
                )
                for fit in fits
            )
            if len(set(ensemble_choices)) == 1:
                selected = ensemble_choices[0]
                fallback_reason = "stable_preference" if selected == stable_preference else None
            else:
                selected = stable_preference
                fallback_reason = "ensemble_disagreement"
        actual = {item.candidate_id: item.latency_ms for item in candidates}
        actual_best = min(actual.values())
        oracle = tuple(
            sorted(
                candidate_id
                for candidate_id, latency in actual.items()
                if latency <= actual_best * (1.0 + practical_tie_fraction)
            )
        )
        decisions.append(
            {
                "scenario_id": scenario_id,
                "seed": seed,
                "equivalence_group": candidates[0].equivalence_group,
                "selected_candidate_id": selected,
                "oracle_candidate_ids": list(oracle),
                "oracle_hit": selected in oracle,
                "regret_percent": (actual[selected] / actual_best - 1.0) * 100.0,
                "fallback_reason": fallback_reason,
                "ensemble_choices": list(ensemble_choices),
            }
        )
    return decisions


def _metrics(decisions: list[dict[str, Any]]) -> dict[str, float | int]:
    """Summarize selection quality without discarding tail failures."""

    if not decisions:
        raise ValueError("Shape-aware metrics require decisions")
    regrets = sorted(float(item["regret_percent"]) for item in decisions)
    p95_index = min(len(regrets) - 1, math.ceil(0.95 * len(regrets)) - 1)
    return {
        "decision_count": len(decisions),
        "oracle_set_hit_rate": statistics.mean(bool(item["oracle_hit"]) for item in decisions),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": regrets[p95_index],
        "maximum_regret_percent": max(regrets),
        "fallback_rate": statistics.mean(item["fallback_reason"] is not None for item in decisions),
    }


def grouped_shape_validation(
    observations: tuple[CalibrationObservation, ...],
    *,
    lambda_grid: tuple[float, ...] = (0.000001, 0.0001, 0.01, 1.0),
    practical_tie_fraction: float = 0.03,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Leave out complete rows-width-match families with no seed leakage."""

    scenarios = sorted({item.scenario_id for item in observations})
    decisions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    for fold_index, scenario_id in enumerate(scenarios, start=1):
        training = tuple(item for item in observations if item.scenario_id != scenario_id)
        testing = tuple(item for item in observations if item.scenario_id == scenario_id)
        fold_decisions: list[dict[str, Any]] = []
        for equivalence_group, stable_preference in SHAPE_STABLE_PREFERENCES.items():
            group_training = tuple(
                item for item in training if item.equivalence_group == equivalence_group
            )
            group_testing = tuple(
                item for item in testing if item.equivalence_group == equivalence_group
            )
            fits = _fit_shape_ensemble(group_training, lambda_grid)
            fold_decisions.extend(
                _evaluate_group(
                    fits,
                    _candidate_support(group_training),
                    group_testing,
                    stable_preference=stable_preference,
                    practical_tie_fraction=practical_tie_fraction,
                )
            )
        decisions.extend(fold_decisions)
        folds.append(
            {
                "held_out_scenario_id": scenario_id,
                "training_scenario_count": len(scenarios) - 1,
                "decision_count": len(fold_decisions),
            }
        )
        if progress_callback is not None:
            progress_callback(fold_index, len(scenarios), scenario_id)

    by_group = {
        group: _metrics([item for item in decisions if item["equivalence_group"] == group])
        for group in SHAPE_STABLE_PREFERENCES
    }
    return {
        **_metrics(decisions),
        "outer_fold_count": len(scenarios),
        "group_metrics": by_group,
        "folds": folds,
        "decisions": decisions,
    }


def _fixed_fallback_validation(
    observations: tuple[CalibrationObservation, ...],
    *,
    practical_tie_fraction: float,
) -> dict[str, float | int]:
    """Evaluate the explicit strong fallback used by the guarded model."""

    decisions: list[dict[str, Any]] = []
    groups: dict[tuple[str, int, str], list[CalibrationObservation]] = {}
    for item in observations:
        groups.setdefault((item.scenario_id, item.seed, item.equivalence_group), []).append(item)
    for (scenario_id, seed, equivalence_group), candidates in sorted(groups.items()):
        selected = SHAPE_STABLE_PREFERENCES[equivalence_group]
        actual = {item.candidate_id: item.latency_ms for item in candidates}
        best = min(actual.values())
        oracle = {
            candidate_id
            for candidate_id, latency in actual.items()
            if latency <= best * (1.0 + practical_tie_fraction)
        }
        decisions.append(
            {
                "scenario_id": scenario_id,
                "seed": seed,
                "equivalence_group": equivalence_group,
                "oracle_hit": selected in oracle,
                "regret_percent": (actual[selected] / best - 1.0) * 100.0,
                "fallback_reason": "fixed_baseline",
            }
        )
    return _metrics(decisions)


def analyze_shape_aware_calibration(
    run_dir: Path,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Evaluate the frozen development design and serialize its full evidence."""

    observations = load_calibration_observations(run_dir)
    validation = grouped_shape_validation(observations, progress_callback=progress_callback)
    fallback = _fixed_fallback_validation(observations, practical_tie_fraction=0.03)
    gates = {
        "minimum_3_percent_oracle_set_hit_rate": validation["oracle_set_hit_rate"] >= 0.8,
        "maximum_mean_regret_percent": validation["mean_regret_percent"] <= 3.0,
        "maximum_p95_regret_percent": validation["p95_regret_percent"] <= 8.0,
        "maximum_regret_percent": validation["maximum_regret_percent"] <= 15.0,
        "mean_regret_not_worse_than_fallback": validation["mean_regret_percent"]
        <= fallback["mean_regret_percent"],
        "p95_regret_not_worse_than_fallback": validation["p95_regret_percent"]
        <= fallback["p95_regret_percent"],
        "all_outer_families_reported": validation["outer_fold_count"] == 18,
    }
    result = {
        "schema_version": 1,
        "status": (
            "PASS_SHAPE_AWARE_CALIBRATION_DEVELOPMENT"
            if all(gates.values())
            else "FAIL_SHAPE_AWARE_CALIBRATION_RETAIN"
        ),
        "gate_checks": gates,
        "grouped_validation": validation,
        "fixed_fallback_validation": fallback,
        "stable_legal_preferences": SHAPE_STABLE_PREFERENCES,
        "model_family": "query_shape_nonnegative_ensemble_v2_development",
        "governance_before_cost": True,
        "direct_winner_classifier_used": False,
        "paper_performance_evidence": False,
        "scientific_boundary": (
            "Development-only analysis on a consumed matrix. A pass authorizes "
            "freezing, not a paper performance claim."
        ),
    }
    _atomic_json(run_dir / "execution_aware_shape_calibration.json", result)
    return result
