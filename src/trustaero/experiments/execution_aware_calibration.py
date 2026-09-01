"""Leakage-safe calibration primitives for the Execution-Aware cost model.

This module contains no DuckDB execution.  It fits a non-negative additive
latency formula and evaluates decisions by complete scenario families, so the
three seeds of one rows/width/match combination can never leak across a fold.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.experiments.execution_flow_features import export_execution_flow_features


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    """One candidate label; features must be available before execution."""

    scenario_id: str
    seed: int
    equivalence_group: str
    candidate_id: str
    latency_ms: float
    features: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.scenario_id, self.equivalence_group, self.candidate_id)
        ):
            raise ValueError("Calibration observation IDs cannot be empty")
        if self.seed < 0 or self.latency_ms <= 0.0 or not math.isfinite(self.latency_ms):
            raise ValueError("Calibration seed and latency are invalid")
        names = [name for name, _value in self.features]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Calibration features must be sorted and unique")
        if any(value < 0.0 or not math.isfinite(value) for _name, value in self.features):
            raise ValueError("Calibration feature values must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class NonnegativeAnalyticFit:
    """One additive fit in original physical-feature units."""

    intercept_ms: float
    coefficients: tuple[tuple[str, float], ...]
    ridge_lambda: float
    iterations: int
    converged: bool

    def predict_ms(self, features: tuple[tuple[str, float], ...]) -> float:
        values = dict(features)
        return self.intercept_ms + sum(
            coefficient * values.get(name, 0.0) for name, coefficient in self.coefficients
        )


def _feature_matrix(
    observations: tuple[CalibrationObservation, ...],
    feature_names: tuple[str, ...],
) -> tuple[list[list[float]], list[float]]:
    rows: list[list[float]] = []
    targets: list[float] = []
    for observation in observations:
        values = dict(observation.features)
        rows.append([values.get(name, 0.0) for name in feature_names])
        targets.append(observation.latency_ms)
    return rows, targets


def fit_nonnegative_analytic_cost(
    observations: tuple[CalibrationObservation, ...],
    *,
    ridge_lambda: float,
    maximum_iterations: int = 5_000,
    tolerance: float = 1e-10,
) -> NonnegativeAnalyticFit:
    """Fit non-negative ridge coefficients by residual-updated coordinates."""

    if not observations:
        raise ValueError("Analytic calibration requires observations")
    if ridge_lambda < 0.0 or maximum_iterations < 1 or tolerance <= 0.0:
        raise ValueError("Analytic calibration controls are invalid")
    feature_names = tuple(
        sorted({name for item in observations for name, value in item.features if value > 0.0})
    )
    if not feature_names:
        raise ValueError("Analytic calibration requires positive work features")
    raw_x, targets = _feature_matrix(observations, feature_names)
    # Positive scaling improves numerical conditioning without changing the
    # non-negative meaning of a coefficient.  We deliberately do not center.
    scales = [
        math.sqrt(sum(row[index] ** 2 for row in raw_x) / len(raw_x)) or 1.0
        for index in range(len(feature_names))
    ]
    x = [[value / scales[index] for index, value in enumerate(row)] for row in raw_x]
    weights = [0.0] * len(feature_names)
    intercept = max(0.0, statistics.mean(targets))
    predictions = [intercept] * len(targets)
    converged = False
    previous_objective = math.inf
    _iteration = 0
    for _iteration in range(1, maximum_iterations + 1):
        new_intercept = max(
            0.0,
            statistics.mean(
                target - (prediction - intercept)
                for target, prediction in zip(targets, predictions, strict=True)
            ),
        )
        intercept_delta = new_intercept - intercept
        if intercept_delta:
            predictions = [value + intercept_delta for value in predictions]
            intercept = new_intercept
        for column in range(len(feature_names)):
            denominator = sum(row[column] ** 2 for row in x) + ridge_lambda
            numerator = sum(
                row[column] * (target - prediction + row[column] * weights[column])
                for row, target, prediction in zip(x, targets, predictions, strict=True)
            )
            updated = max(0.0, numerator / denominator) if denominator else 0.0
            delta = updated - weights[column]
            if delta:
                predictions = [
                    prediction + row[column] * delta
                    for row, prediction in zip(x, predictions, strict=True)
                ]
                weights[column] = updated
        objective = sum(
            (target - prediction) ** 2
            for target, prediction in zip(targets, predictions, strict=True)
        ) + ridge_lambda * sum(weight**2 for weight in weights)
        # With correlated cost components, coefficients can exchange small
        # amounts of weight long after predictions have stabilized.  The
        # regularized objective is the scientifically relevant convergence
        # quantity and is invariant to feature units after scaling.
        if math.isfinite(previous_objective) and abs(
            previous_objective - objective
        ) <= tolerance * max(1.0, previous_objective):
            converged = True
            break
        previous_objective = objective
    coefficients = tuple(
        (name, weight / scale)
        for name, weight, scale in zip(feature_names, weights, scales, strict=True)
    )
    return NonnegativeAnalyticFit(
        intercept_ms=intercept,
        coefficients=coefficients,
        ridge_lambda=ridge_lambda,
        iterations=_iteration,
        converged=converged,
    )


def mean_log_latency_error(
    fit: NonnegativeAnalyticFit,
    observations: tuple[CalibrationObservation, ...],
) -> float:
    """Use symmetric log error so large queries do not dominate lambda choice."""

    return statistics.mean(
        abs(math.log(max(fit.predict_ms(item.features), 1e-12) / item.latency_ms))
        for item in observations
    )


def select_lambda_inside_training(
    observations: tuple[CalibrationObservation, ...],
    *,
    lambda_grid: tuple[float, ...],
) -> float:
    """Select lambda on complete deterministic inner scenario groups."""

    scenarios = sorted({item.scenario_id for item in observations})
    if len(scenarios) < 3:
        raise ValueError("Nested calibration requires at least three training scenarios")
    validation_ids = set(scenarios[::5] or scenarios[-1:])
    training = tuple(item for item in observations if item.scenario_id not in validation_ids)
    validation = tuple(item for item in observations if item.scenario_id in validation_ids)
    scores: list[tuple[float, float]] = []
    for ridge_lambda in lambda_grid:
        fit = fit_nonnegative_analytic_cost(training, ridge_lambda=ridge_lambda)
        if not fit.converged:
            scores.append((math.inf, ridge_lambda))
        else:
            scores.append((mean_log_latency_error(fit, validation), ridge_lambda))
    return min(scores)[1]


@dataclass(frozen=True, slots=True)
class SelectionEvaluation:
    scenario_id: str
    seed: int
    equivalence_group: str
    selected_candidate_id: str
    oracle_candidate_ids: tuple[str, ...]
    oracle_hit: bool
    regret_percent: float


def evaluate_fit_selections(
    fit: NonnegativeAnalyticFit,
    observations: tuple[CalibrationObservation, ...],
    *,
    stable_preferences: dict[str, str],
    practical_tie_fraction: float,
) -> tuple[SelectionEvaluation, ...]:
    """Evaluate one model only among result-equivalent candidates."""

    groups: dict[tuple[str, int, str], list[CalibrationObservation]] = {}
    for observation in observations:
        key = (observation.scenario_id, observation.seed, observation.equivalence_group)
        groups.setdefault(key, []).append(observation)
    results: list[SelectionEvaluation] = []
    for (scenario_id, seed, equivalence_group), candidates in sorted(groups.items()):
        if len(candidates) < 2:
            raise ValueError("Cost selection requires at least two equivalent candidates")
        predicted = {item.candidate_id: fit.predict_ms(item.features) for item in candidates}
        predicted_best = min(predicted.values())
        predicted_set = {
            candidate_id
            for candidate_id, value in predicted.items()
            if value <= predicted_best * (1.0 + practical_tie_fraction)
        }
        preferred = stable_preferences.get(equivalence_group)
        selected = (
            preferred
            if preferred in predicted_set
            else min(predicted_set, key=lambda item: (predicted[item], item))
        )
        actual = {item.candidate_id: item.latency_ms for item in candidates}
        actual_best = min(actual.values())
        oracle = tuple(
            sorted(
                candidate_id
                for candidate_id, value in actual.items()
                if value <= actual_best * (1.0 + practical_tie_fraction)
            )
        )
        results.append(
            SelectionEvaluation(
                scenario_id=scenario_id,
                seed=seed,
                equivalence_group=equivalence_group,
                selected_candidate_id=selected,
                oracle_candidate_ids=oracle,
                oracle_hit=selected in oracle,
                regret_percent=(actual[selected] / actual_best - 1.0) * 100.0,
            )
        )
    return tuple(results)


def grouped_outer_validation(
    observations: tuple[CalibrationObservation, ...],
    *,
    lambda_grid: tuple[float, ...],
    stable_preferences: dict[str, str],
    practical_tie_fraction: float = 0.03,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run leave-one-complete-scenario-family-out model development."""

    scenarios = sorted({item.scenario_id for item in observations})
    evaluations: list[SelectionEvaluation] = []
    fold_metadata: list[dict[str, Any]] = []
    for fold_index, scenario_id in enumerate(scenarios, start=1):
        training = tuple(item for item in observations if item.scenario_id != scenario_id)
        testing = tuple(item for item in observations if item.scenario_id == scenario_id)
        selected_lambda = select_lambda_inside_training(training, lambda_grid=lambda_grid)
        fit = fit_nonnegative_analytic_cost(training, ridge_lambda=selected_lambda)
        if not fit.converged:
            raise ValueError(f"Nonnegative analytic fit did not converge: {scenario_id}")
        fold_results = evaluate_fit_selections(
            fit,
            testing,
            stable_preferences=stable_preferences,
            practical_tie_fraction=practical_tie_fraction,
        )
        evaluations.extend(fold_results)
        fold_metadata.append(
            {
                "held_out_scenario_id": scenario_id,
                "training_scenario_count": len(scenarios) - 1,
                "selected_ridge_lambda": selected_lambda,
                "fit_iterations": fit.iterations,
                "held_out_decision_count": len(fold_results),
            }
        )
        if progress_callback is not None:
            progress_callback(fold_index, len(scenarios), scenario_id)
    regrets = sorted(item.regret_percent for item in evaluations)
    p95_index = min(len(regrets) - 1, math.ceil(0.95 * len(regrets)) - 1)
    return {
        "outer_fold_count": len(scenarios),
        "decision_count": len(evaluations),
        "oracle_set_hit_rate": statistics.mean(item.oracle_hit for item in evaluations),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": regrets[p95_index],
        "maximum_regret_percent": max(regrets),
        "folds": fold_metadata,
        "decisions": [
            {
                "scenario_id": item.scenario_id,
                "seed": item.seed,
                "equivalence_group": item.equivalence_group,
                "selected_candidate_id": item.selected_candidate_id,
                "oracle_candidate_ids": list(item.oracle_candidate_ids),
                "oracle_hit": item.oracle_hit,
                "regret_percent": item.regret_percent,
            }
            for item in evaluations
        ],
    }


STABLE_PREFERENCES = {
    "column_pruning": "join_key_only_aggregate",
    "mask_output": "postjoin_mask_fused_output",
    "mask_aggregate": "postjoin_mask_fused_aggregate",
    "mask_sorted_output": "postjoin_mask_fused_sorted_output",
}


def _basis_features(values: dict[str, float]) -> tuple[tuple[str, float], ...]:
    """Merge features that this calibration design cannot identify separately."""

    output = dict(values)
    for state in ("raw", "masked"):
        read = output.pop(f"materialization.{state}.read_gib", 0.0)
        write = output.pop(f"materialization.{state}.write_gib", 0.0)
        output[f"materialization.{state}.roundtrip_gib"] = read + write
    # EA-1 uses a fixed 16-byte build schema, making build rows and bytes
    # exactly proportional. Keep the engine-facing row term and freeze the
    # unidentifiable payload coefficient to zero in the serialized model.
    output.pop("join.build_payload_gib", None)
    return tuple(sorted(output.items()))


def load_calibration_observations(run_dir: Path) -> tuple[CalibrationObservation, ...]:
    """Bind complete DuckDB labels to independently exported work features."""

    run_dir = run_dir.resolve()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_EXECUTION_FLOW_AUDIT":
        raise ValueError("Calibration requires a complete passed execution-flow run")
    feature_path = export_execution_flow_features(run_dir)
    with feature_path.open(newline="", encoding="utf-8") as handle:
        feature_rows = list(csv.DictReader(handle))
    with (run_dir / "variant_summary.csv").open(newline="", encoding="utf-8") as handle:
        label_rows = list(csv.DictReader(handle))
    identities = {
        "unit_id",
        "row_count",
        "identifier_width",
        "match_rate",
        "seed",
        "variant_id",
        "equivalence_group",
        "physical_plan_id",
        "raw_rows_exposed_to_join",
        "raw_rows_materialized",
        "masked_rows_materialized",
    }
    features_by_key: dict[tuple[str, str], dict[str, float]] = {}
    for row in feature_rows:
        key = (row["unit_id"], row["variant_id"])
        if key in features_by_key:
            raise ValueError(f"Duplicate calibration feature key: {key}")
        features_by_key[key] = {
            name: float(value or 0.0) for name, value in row.items() if name not in identities
        }
    observations: list[CalibrationObservation] = []
    for row in label_rows:
        key = (row["unit_id"], row["variant_id"])
        if key not in features_by_key:
            raise ValueError(f"Calibration label has no feature row: {key}")
        scenario_id = f"n{row['row_count']}-w{row['identifier_width']}-m{row['match_rate']}"
        observations.append(
            CalibrationObservation(
                scenario_id=scenario_id,
                seed=int(row["seed"]),
                equivalence_group=row["equivalence_group"],
                candidate_id=row["variant_id"],
                latency_ms=float(row["median_latency_ms"]),
                features=_basis_features(features_by_key.pop(key)),
            )
        )
    if features_by_key:
        raise ValueError("Calibration feature rows exist without latency labels")
    return tuple(observations)


def analyze_execution_aware_calibration(
    run_dir: Path,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run the frozen grouped evaluation and write auditable development artifacts."""

    observations = load_calibration_observations(run_dir)
    validation = grouped_outer_validation(
        observations,
        lambda_grid=(0.000001, 0.0001, 0.01, 1.0),
        stable_preferences=STABLE_PREFERENCES,
        progress_callback=progress_callback,
    )
    gates = {
        "minimum_3_percent_oracle_set_hit_rate": validation["oracle_set_hit_rate"] >= 0.8,
        "maximum_mean_regret_percent": validation["mean_regret_percent"] <= 3.0,
        "maximum_p95_regret_percent": validation["p95_regret_percent"] <= 8.0,
        "maximum_regret_percent": validation["maximum_regret_percent"] <= 15.0,
        "all_outer_families_reported": validation["outer_fold_count"] == 18,
    }
    final_lambda = select_lambda_inside_training(
        observations,
        lambda_grid=(0.000001, 0.0001, 0.01, 1.0),
    )
    final_fit = fit_nonnegative_analytic_cost(observations, ridge_lambda=final_lambda)
    if not final_fit.converged:
        raise ValueError("Final Execution-Aware analytic model did not converge")
    model = {
        "model_type": "execution_aware_analytic_cost_v1_development",
        "intercept_ms": final_fit.intercept_ms,
        "basis_coefficients_ms_per_unit": dict(final_fit.coefficients),
        "ridge_lambda": final_fit.ridge_lambda,
        "stable_legal_preferences": STABLE_PREFERENCES,
        "practical_tie_fraction": 0.03,
        "governance_before_cost": True,
        "direct_winner_classifier_used": False,
        "basis_merges": {
            "materialization.raw.roundtrip_gib": [
                "materialization.raw.read_gib",
                "materialization.raw.write_gib",
            ],
            "materialization.masked.roundtrip_gib": [
                "materialization.masked.read_gib",
                "materialization.masked.write_gib",
            ],
            "join.build_payload_gib": "fixed_zero_due_to_exact_collinearity_with_build_rows",
        },
        "optimizer_holdout_evidence": False,
    }
    result = {
        "schema_version": 1,
        "status": (
            "PASS_EXECUTION_AWARE_CALIBRATION_DEVELOPMENT"
            if all(gates.values())
            else "FAIL_EXECUTION_AWARE_CALIBRATION_RETAIN"
        ),
        "observation_count": len(observations),
        "scenario_count": len({item.scenario_id for item in observations}),
        "gate_checks": gates,
        "grouped_validation": validation,
        "model": model,
        "paper_performance_evidence": False,
        "scientific_boundary": (
            "Development-only grouped calibration. Passing authorizes model freezing "
            "and a new holdout protocol, not an optimizer superiority claim."
        ),
    }
    _atomic_json(run_dir / "execution_aware_model.json", model)
    _atomic_json(run_dir / "execution_aware_calibration.json", result)
    return result
