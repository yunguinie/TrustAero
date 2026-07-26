"""Candidate-specific analytic calibration for Execution-Aware planning.

Each physical candidate gets its own non-negative cost formula.  This matters
because fused execution and materialized pipelines can have different effective
throughputs even when they expose similarly named logical work.  The optimizer
still compares predicted costs rather than learning a direct winner label.
"""

from __future__ import annotations

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
from trustaero.experiments.execution_aware_shape_calibration import (
    SHAPE_MAXIMUM_ITERATIONS,
    SHAPE_OBJECTIVE_TOLERANCE,
    SHAPE_STABLE_PREFERENCES,
    _candidate_support,
    _fixed_fallback_validation,
    _inside_support,
    _metrics,
)
from trustaero.experiments.execution_flow_audit import _atomic_json

DEPLOYABLE_STABLE_PREFERENCES = {
    group: candidate
    for group, candidate in SHAPE_STABLE_PREFERENCES.items()
    if group != "column_pruning"
}


def _fit_candidate_ensembles(
    observations: tuple[CalibrationObservation, ...],
    lambda_grid: tuple[float, ...],
) -> dict[str, tuple[NonnegativeAnalyticFit, ...]]:
    """Fit independent analytic rate views for every physical candidate."""

    by_candidate: dict[str, list[CalibrationObservation]] = {}
    for observation in observations:
        by_candidate.setdefault(observation.candidate_id, []).append(observation)
    result: dict[str, tuple[NonnegativeAnalyticFit, ...]] = {}
    for candidate_id, raw_items in sorted(by_candidate.items()):
        items = tuple(raw_items)
        fits: list[NonnegativeAnalyticFit] = []
        for ridge_lambda in lambda_grid:
            fit = fit_nonnegative_analytic_cost(
                items,
                ridge_lambda=ridge_lambda,
                maximum_iterations=SHAPE_MAXIMUM_ITERATIONS,
                tolerance=SHAPE_OBJECTIVE_TOLERANCE,
            )
            if not fit.converged:
                raise ValueError(
                    "Candidate-specific analytic view did not converge: "
                    f"candidate={candidate_id}, lambda={ridge_lambda}, "
                    f"iterations={fit.iterations}"
                )
            fits.append(fit)
        result[candidate_id] = tuple(fits)
    return result


def _candidate_specific_choice(
    fits: dict[str, tuple[NonnegativeAnalyticFit, ...]],
    candidates: tuple[CalibrationObservation, ...],
    *,
    view_index: int,
    stable_preference: str,
    practical_tie_fraction: float,
) -> str:
    """Compare costs emitted by the same regularization view."""

    predicted = {
        item.candidate_id: fits[item.candidate_id][view_index].predict_ms(item.features)
        for item in candidates
    }
    best = min(predicted.values())
    practical_set = {
        candidate_id
        for candidate_id, latency in predicted.items()
        if latency <= best * (1.0 + practical_tie_fraction)
    }
    if stable_preference in practical_set:
        return stable_preference
    return min(practical_set, key=lambda item: (predicted[item], item))


def _evaluate_candidate_ensembles(
    fits: dict[str, tuple[NonnegativeAnalyticFit, ...]],
    support: dict[str, dict[str, tuple[float, float]]],
    observations: tuple[CalibrationObservation, ...],
    *,
    stable_preference: str,
    practical_tie_fraction: float,
) -> list[dict[str, Any]]:
    """Select only with in-support unanimous candidate-cost predictions."""

    by_seed: dict[tuple[str, int], list[CalibrationObservation]] = {}
    for observation in observations:
        by_seed.setdefault((observation.scenario_id, observation.seed), []).append(observation)
    decisions: list[dict[str, Any]] = []
    for (scenario_id, seed), raw_candidates in sorted(by_seed.items()):
        candidates = tuple(raw_candidates)
        if stable_preference not in {item.candidate_id for item in candidates}:
            raise ValueError("Stable candidate-specific fallback is absent")
        guard_reason: str | None
        view_choices: tuple[str, ...]
        if not all(_inside_support(item, support) for item in candidates):
            selected = stable_preference
            view_choices = ()
            guard_reason = "out_of_support"
        else:
            view_count = len(next(iter(fits.values())))
            if any(len(candidate_fits) != view_count for candidate_fits in fits.values()):
                raise ValueError("Candidate-specific ensembles have unequal views")
            view_choices = tuple(
                _candidate_specific_choice(
                    fits,
                    candidates,
                    view_index=view_index,
                    stable_preference=stable_preference,
                    practical_tie_fraction=practical_tie_fraction,
                )
                for view_index in range(view_count)
            )
            if len(set(view_choices)) == 1:
                selected = view_choices[0]
                guard_reason = None
            else:
                selected = stable_preference
                guard_reason = "ensemble_disagreement"
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
                "guard_reason": guard_reason,
                "view_choices": list(view_choices),
                "selected_stable_candidate": selected == stable_preference,
            }
        )
    return decisions


def _candidate_metrics(decisions: list[dict[str, Any]]) -> dict[str, float | int]:
    """Keep guard activation separate from selecting the stable candidate."""

    result = _metrics([{**item, "fallback_reason": item["guard_reason"]} for item in decisions])
    result["guard_trigger_rate"] = statistics.mean(
        item["guard_reason"] is not None for item in decisions
    )
    result["stable_candidate_selection_rate"] = statistics.mean(
        bool(item["selected_stable_candidate"]) for item in decisions
    )
    result["model_override_rate"] = 1.0 - float(result["stable_candidate_selection_rate"])
    # Remove the older ambiguous name inherited from the shared helper.
    result.pop("fallback_rate", None)
    return result


def grouped_candidate_validation(
    observations: tuple[CalibrationObservation, ...],
    *,
    lambda_grid: tuple[float, ...] = (0.000001, 0.0001, 0.01, 1.0),
    practical_tie_fraction: float = 0.03,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Evaluate candidate-specific formulas on complete held-out families."""

    scenarios = sorted({item.scenario_id for item in observations})
    decisions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    for fold_index, scenario_id in enumerate(scenarios, start=1):
        training = tuple(item for item in observations if item.scenario_id != scenario_id)
        testing = tuple(item for item in observations if item.scenario_id == scenario_id)
        fits = _fit_candidate_ensembles(training, lambda_grid)
        support = _candidate_support(training)
        fold_decisions: list[dict[str, Any]] = []
        for equivalence_group, stable_preference in DEPLOYABLE_STABLE_PREFERENCES.items():
            group_testing = tuple(
                item for item in testing if item.equivalence_group == equivalence_group
            )
            group_candidate_ids = {item.candidate_id for item in group_testing}
            group_fits = {candidate_id: fits[candidate_id] for candidate_id in group_candidate_ids}
            group_support = {
                candidate_id: support[candidate_id] for candidate_id in group_candidate_ids
            }
            fold_decisions.extend(
                _evaluate_candidate_ensembles(
                    group_fits,
                    group_support,
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

    return {
        **_candidate_metrics(decisions),
        "outer_fold_count": len(scenarios),
        "group_metrics": {
            group: _candidate_metrics(
                [item for item in decisions if item["equivalence_group"] == group]
            )
            for group in DEPLOYABLE_STABLE_PREFERENCES
        },
        "folds": folds,
        "decisions": decisions,
    }


def analyze_candidate_specific_calibration(
    run_dir: Path,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Write development-only evidence for candidate-specific cost formulas."""

    all_observations = load_calibration_observations(run_dir)
    # Column-pruning variants are mechanism-audit controls, not deployable
    # alternatives for a governed logical query. Keeping this boundary explicit
    # prevents a diagnostic stress plan from entering optimizer metrics.
    observations = tuple(
        item for item in all_observations if item.equivalence_group in DEPLOYABLE_STABLE_PREFERENCES
    )
    validation = grouped_candidate_validation(observations, progress_callback=progress_callback)
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
            "PASS_CANDIDATE_SPECIFIC_CALIBRATION_DEVELOPMENT"
            if all(gates.values())
            else "FAIL_CANDIDATE_SPECIFIC_CALIBRATION_RETAIN"
        ),
        "gate_checks": gates,
        "grouped_validation": validation,
        "fixed_fallback_validation": fallback,
        "stable_legal_preferences": DEPLOYABLE_STABLE_PREFERENCES,
        "excluded_mechanism_only_groups": ["column_pruning"],
        "model_family": ("deployment_scoped_candidate_specific_cost_ensemble_development"),
        "governance_before_cost": True,
        "direct_winner_classifier_used": False,
        "paper_performance_evidence": False,
        "scientific_boundary": (
            "Development-only analysis on a consumed matrix. Candidate-specific "
            "success must be confirmed on a newly frozen scenario matrix."
        ),
    }
    _atomic_json(run_dir / "execution_aware_deployment_candidate_calibration.json", result)
    return result
