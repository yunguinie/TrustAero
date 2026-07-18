"""Fit and evaluate the frozen mechanism-based Mask cost formula.

Microbenchmark scenario groups are aggregated before fitting.  End-to-end
workload winners are used only for the final development evaluation, never as
training targets for the mechanism coefficients.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.optimizer_v2 import (
    MaskWorkloadObservation,
    load_mask_workload_observations,
)
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_mechanism import (
    HASH_FEATURE_NAMES,
    JOIN_FEATURE_NAMES,
    MATERIALIZATION_FEATURE_NAMES,
    MechanismMaskCostModel,
    NonnegativeMechanismCost,
    choose_mask_placement_by_mechanism,
)


@dataclass(frozen=True)
class MechanismObservation:
    """One seed-aggregated microbenchmark scenario group."""

    component_name: str
    group_id: str
    features: tuple[float, ...]
    target_ms: float
    source_run_id: str
    replicate_count: int

    def __post_init__(self) -> None:
        if not self.component_name or not self.group_id or not self.source_run_id:
            raise ValueError("Mechanism observation identifiers cannot be empty")
        if not self.features or any(value < 0.0 for value in self.features):
            raise ValueError("Mechanism observation features must be non-negative")
        if self.target_ms <= 0.0 or self.replicate_count <= 0:
            raise ValueError("Mechanism targets and replicate counts must be positive")


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing experiment artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for name in row:
            if name not in seen:
                seen.add(name)
                fieldnames.append(name)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _completed_run_id(run_dir: Path) -> str:
    summary = _read_object(run_dir / "summary.json")
    if summary.get("status") != "complete":
        raise ValueError(f"Mechanism run is not complete: {run_dir}")
    if summary.get("all_validations_passed") is not True:
        raise ValueError(f"Mechanism run failed validation: {run_dir}")
    return str(summary.get("run_id", run_dir.name))


def load_hash_and_materialization_observations(
    run_dir_value: str | Path,
) -> dict[str, list[MechanismObservation]]:
    """Aggregate pilot seed replicates by component, scale, and payload width."""

    run_dir = Path(run_dir_value).resolve()
    run_id = _completed_run_id(run_dir)
    wanted = {
        "hash_incremental": ("sha256", HASH_FEATURE_NAMES),
        "materialization_roundtrip": (
            "materialization_roundtrip",
            MATERIALIZATION_FEATURE_NAMES,
        ),
    }
    grouped: dict[tuple[str, int, int], list[float]] = {}
    for row in _read_csv(run_dir / "paired_costs.csv"):
        derived = row["derived_component"]
        if derived not in wanted:
            continue
        key = (derived, int(row["row_count"]), int(row["identifier_width"]))
        grouped.setdefault(key, []).append(float(row["median_paired_cost_ms"]))
    output: dict[str, list[MechanismObservation]] = {
        "sha256": [],
        "materialization_roundtrip": [],
    }
    mib = float(1024 * 1024)
    for (derived, rows, width), targets in sorted(grouped.items()):
        component_name, _feature_names = wanted[derived]
        output[component_name].append(
            MechanismObservation(
                component_name=component_name,
                group_id=f"{component_name}/n{rows}/w{width}",
                features=(rows / 100_000.0, rows * width / mib),
                target_ms=statistics.median(targets),
                source_run_id=run_id,
                replicate_count=len(targets),
            )
        )
    if any(len(values) < 3 for values in output.values()):
        raise ValueError("Mechanism pilot lacks enough hash/materialization groups")
    return output


def load_join_observations(run_dir_value: str | Path) -> list[MechanismObservation]:
    """Aggregate HASH_JOIN profiles over seeds and widths per cardinality group.

    Width is intentionally omitted from both the group key and feature vector.
    This prevents the repeated width points from overweighting a cardinality
    setting after operator profiling found no stable width effect.
    """

    run_dir = Path(run_dir_value).resolve()
    run_id = _completed_run_id(run_dir)
    grouped: dict[tuple[int, int], list[float]] = {}
    for row in _read_csv(run_dir / "operator_summary.csv"):
        if row["component"] != "join_payload" or row["operator_name"] != "HASH_JOIN":
            continue
        input_rows = int(row["row_count"])
        output_rows = int(row["actual_cardinality"])
        grouped.setdefault((input_rows, output_rows), []).append(
            float(row["median_operator_timing_ms"])
        )
    output = [
        MechanismObservation(
            component_name="hash_join",
            group_id=f"hash_join/n{input_rows}/o{output_rows}",
            features=(input_rows / 100_000.0, output_rows / 100_000.0),
            target_ms=statistics.median(targets),
            source_run_id=run_id,
            replicate_count=len(targets),
        )
        for (input_rows, output_rows), targets in sorted(grouped.items())
    ]
    if len(output) < 3:
        raise ValueError("Join calibration lacks enough cardinality groups")
    return output


def fit_nonnegative_mechanism_cost(
    observations: list[MechanismObservation],
    *,
    feature_names: tuple[str, ...],
    ridge_lambda: float = 0.01,
    max_iterations: int = 20_000,
    tolerance: float = 1e-10,
) -> NonnegativeMechanismCost:
    """Fit intercept and coefficients with non-negative coordinate descent."""

    if len(observations) <= len(feature_names):
        raise ValueError("Mechanism fitting needs more groups than features")
    if ridge_lambda < 0.0 or max_iterations < 1 or tolerance <= 0.0:
        raise ValueError("Mechanism fitting parameters are invalid")
    component_names = {item.component_name for item in observations}
    if len(component_names) != 1:
        raise ValueError("A mechanism fit may contain only one component")
    if any(len(item.features) != len(feature_names) for item in observations):
        raise ValueError("Mechanism observation has an incompatible feature vector")
    coefficients = [0.0] * len(feature_names)
    intercept = max(0.0, statistics.mean(item.target_ms for item in observations))
    for _iteration in range(max_iterations):
        largest_change = 0.0
        updated_intercept = max(
            0.0,
            statistics.mean(
                item.target_ms
                - sum(
                    value * coefficient
                    for value, coefficient in zip(item.features, coefficients, strict=True)
                )
                for item in observations
            ),
        )
        largest_change = max(largest_change, abs(updated_intercept - intercept))
        intercept = updated_intercept
        for index in range(len(feature_names)):
            numerator = 0.0
            denominator = ridge_lambda
            for item in observations:
                residual_without_term = (
                    item.target_ms
                    - intercept
                    - sum(
                        item.features[other] * coefficients[other]
                        for other in range(len(feature_names))
                        if other != index
                    )
                )
                numerator += item.features[index] * residual_without_term
                denominator += item.features[index] ** 2
            updated = max(0.0, numerator / denominator) if denominator else 0.0
            largest_change = max(largest_change, abs(updated - coefficients[index]))
            coefficients[index] = updated
        if largest_change < tolerance:
            break
    return NonnegativeMechanismCost(
        component_name=observations[0].component_name,
        feature_names=feature_names,
        intercept_ms=intercept,
        coefficients=tuple(coefficients),
        ridge_lambda=ridge_lambda,
        training_group_count=len(observations),
        source_run_ids=tuple(sorted({item.source_run_id for item in observations})),
    )


def cross_validate_mechanism_cost(
    observations: list[MechanismObservation],
    *,
    feature_names: tuple[str, ...],
    ridge_lambda: float = 0.01,
) -> list[dict[str, Any]]:
    """Leave one complete mechanism scenario group out per fold."""

    output: list[dict[str, Any]] = []
    for held_out in observations:
        training = [item for item in observations if item.group_id != held_out.group_id]
        model = fit_nonnegative_mechanism_cost(
            training, feature_names=feature_names, ridge_lambda=ridge_lambda
        )
        predicted = model.predict_ms(held_out.features)
        relative_error = abs(predicted - held_out.target_ms) / held_out.target_ms
        output.append(
            {
                "component_name": held_out.component_name,
                "holdout_group": held_out.group_id,
                "source_run_id": held_out.source_run_id,
                "replicate_count": held_out.replicate_count,
                "actual_ms": held_out.target_ms,
                "predicted_ms": predicted,
                "absolute_error_ms": abs(predicted - held_out.target_ms),
                "absolute_relative_error_percent": relative_error * 100.0,
            }
        )
    return output


def _percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(fraction * len(values)) - 1]


def _component_cv_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [float(row["absolute_relative_error_percent"]) for row in rows]
    return {
        "group_count": len(rows),
        "median_absolute_relative_error_percent": statistics.median(errors),
        "mean_absolute_relative_error_percent": statistics.mean(errors),
        "p95_absolute_relative_error_percent": _percentile(errors, 0.95),
        "max_absolute_relative_error_percent": max(errors),
    }


def fit_mechanism_mask_model(
    pilot_run_dir: str | Path,
    join_run_dir: str | Path,
    *,
    ridge_lambda: float = 0.01,
) -> tuple[MechanismMaskCostModel, list[dict[str, Any]], dict[str, Any]]:
    """Fit all three independent mechanism costs and grouped diagnostics."""

    components = load_hash_and_materialization_observations(pilot_run_dir)
    components["hash_join"] = load_join_observations(join_run_dir)
    feature_names = {
        "sha256": HASH_FEATURE_NAMES,
        "materialization_roundtrip": MATERIALIZATION_FEATURE_NAMES,
        "hash_join": JOIN_FEATURE_NAMES,
    }
    cv_rows: list[dict[str, Any]] = []
    models: dict[str, NonnegativeMechanismCost] = {}
    for component_name in ("sha256", "materialization_roundtrip", "hash_join"):
        observations = components[component_name]
        names = feature_names[component_name]
        cv_rows.extend(
            cross_validate_mechanism_cost(
                observations, feature_names=names, ridge_lambda=ridge_lambda
            )
        )
        models[component_name] = fit_nonnegative_mechanism_cost(
            observations, feature_names=names, ridge_lambda=ridge_lambda
        )
    grouped_cv: dict[str, list[dict[str, Any]]] = {}
    for row in cv_rows:
        grouped_cv.setdefault(str(row["component_name"]), []).append(row)
    return (
        MechanismMaskCostModel(
            hash_cost=models["sha256"],
            materialization_cost=models["materialization_roundtrip"],
            join_cost=models["hash_join"],
        ),
        cv_rows,
        {name: _component_cv_summary(rows) for name, rows in sorted(grouped_cv.items())},
    )


def _prediction_row(
    observation: MaskWorkloadObservation,
    model: MechanismMaskCostModel,
) -> dict[str, Any]:
    decision = choose_mask_placement_by_mechanism(observation.features, model)
    actual = observation.observed_log_early_late_ratio
    oracle = MaskPlacement.EARLY if actual < 0.0 else MaskPlacement.LATE
    if decision.placement is MaskPlacement.EARLY:
        regret_ratio = max(1.0, math.exp(actual))
        speedup_late = math.exp(-actual)
        speedup_early = 1.0
    else:
        regret_ratio = max(1.0, math.exp(-actual))
        speedup_late = 1.0
        speedup_early = math.exp(actual)
    early = decision.estimated_early_latency_ms
    late = decision.estimated_late_latency_ms
    if early <= 0.0 or late <= 0.0:
        raise ValueError("Mechanism model produced a non-positive candidate estimate")
    return {
        "evaluation_scheme": "mechanism_formula_fixed_development",
        "holdout_group": "independent_microbenchmarks",
        "workload_id": observation.workload_id,
        "scenario_group_id": observation.scenario_group_id,
        "scenario_id": observation.scenario_id,
        "row_count": observation.row_count,
        "seed_count": observation.seed_count,
        "join_input_rows": observation.features.join_input_rows,
        "identifier_width_bytes": observation.features.identifier_width_bytes,
        "join_match_rate": observation.features.join_match_rate,
        "observed_log_early_late_ratio": actual,
        "predicted_log_early_late_ratio": math.log(early / late),
        "selected_placement": decision.placement.value,
        "oracle_placement": oracle.value,
        "exact_top1": decision.placement is oracle,
        "within_tie_threshold": regret_ratio - 1.0 <= observation.tie_threshold_fraction,
        "regret_percent": (regret_ratio - 1.0) * 100.0,
        "speedup_vs_fixed_late_ratio": speedup_late,
        "speedup_vs_fixed_early_ratio": speedup_early,
        "estimated_early_latency_ms": early,
        "estimated_late_latency_ms": late,
        "early_sha256_ms": decision.early_components_ms["sha256_ms"],
        "early_payload_movement_ms": decision.early_components_ms["payload_movement_ms"],
        "early_hash_join_ms": decision.early_components_ms["hash_join_ms"],
        "late_sha256_ms": decision.late_components_ms["sha256_ms"],
        "late_payload_movement_ms": decision.late_components_ms["payload_movement_ms"],
        "late_hash_join_ms": decision.late_components_ms["hash_join_ms"],
        "decision_reason_code": decision.reason_code,
    }


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def _scheme_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    regrets = [float(row["regret_percent"]) for row in rows]
    return {
        "observation_count": len(rows),
        "exact_top1_count": sum(_boolean(row["exact_top1"]) for row in rows),
        "exact_top1_rate": sum(_boolean(row["exact_top1"]) for row in rows) / len(rows),
        "within_tie_count": sum(_boolean(row["within_tie_threshold"]) for row in rows),
        "within_tie_rate": sum(_boolean(row["within_tie_threshold"]) for row in rows) / len(rows),
        "mean_regret_percent": statistics.mean(regrets),
        "median_regret_percent": statistics.median(regrets),
        "p95_regret_percent": _percentile(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "geometric_speedup_vs_fixed_late_ratio": math.exp(
            statistics.mean(math.log(float(row["speedup_vs_fixed_late_ratio"])) for row in rows)
        ),
        "geometric_speedup_vs_fixed_early_ratio": math.exp(
            statistics.mean(math.log(float(row["speedup_vs_fixed_early_ratio"])) for row in rows)
        ),
    }


def audit_mechanism_monotonicity(
    model: MechanismMaskCostModel,
    *,
    row_counts: tuple[int, ...],
    identifier_widths: tuple[int, ...],
    match_rates: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 0.75, 1.0),
) -> dict[str, Any]:
    """Check that higher Join survival never makes early Mask less attractive."""

    violations: list[dict[str, Any]] = []
    comparisons = 0
    for rows in row_counts:
        for width in identifier_widths:
            previous: float | None = None
            for match_rate in match_rates:
                features = MaskPlacementFeatures(
                    join_input_rows=rows,
                    identifier_width_bytes=width,
                    join_match_rate=match_rate,
                )
                early = model.predict_candidate_ms(features, MaskPlacement.EARLY)
                late = model.predict_candidate_ms(features, MaskPlacement.LATE)
                score = math.log(early / late)
                if previous is not None:
                    comparisons += 1
                    if score > previous + 1e-9:
                        violations.append(
                            {
                                "row_count": rows,
                                "identifier_width_bytes": width,
                                "match_rate": match_rate,
                                "previous_score": previous,
                                "score": score,
                            }
                        )
                previous = score
    return {
        "expected_direction": "log(early/late) nonincreasing with match_rate",
        "comparison_count": comparisons,
        "violation_count": len(violations),
        "passes": not violations,
        "violations": violations,
    }


def audit_governance_hard_constraints(model: MechanismMaskCostModel) -> dict[str, Any]:
    """Inject feasibility constraints and ensure cost never overrides them."""

    violations: list[str] = []
    cases = (
        (
            "early_illegal",
            MaskPlacementFeatures(
                join_input_rows=100_000,
                identifier_width_bytes=1024,
                join_match_rate=0.5,
                early_mask_legal=False,
            ),
            MaskPlacement.LATE,
        ),
        (
            "late_illegal",
            MaskPlacementFeatures(
                join_input_rows=100_000,
                identifier_width_bytes=1024,
                join_match_rate=0.5,
                late_mask_legal=False,
            ),
            MaskPlacement.EARLY,
        ),
        (
            "raw_exposure_zero",
            MaskPlacementFeatures(
                join_input_rows=100_000,
                identifier_width_bytes=1024,
                join_match_rate=0.5,
                max_raw_exposure_rows=0,
            ),
            MaskPlacement.EARLY,
        ),
    )
    for case_id, features, expected in cases:
        if choose_mask_placement_by_mechanism(features, model).placement is not expected:
            violations.append(case_id)
    rejected_both = False
    try:
        choose_mask_placement_by_mechanism(
            MaskPlacementFeatures(
                join_input_rows=100_000,
                identifier_width_bytes=1024,
                join_match_rate=0.5,
                early_mask_legal=False,
                late_mask_legal=False,
            ),
            model,
        )
    except ValueError:
        rejected_both = True
    if not rejected_both:
        violations.append("both_illegal_not_rejected")
    return {
        "injected_case_count": len(cases) + 1,
        "violation_count": len(violations),
        "passes": not violations,
        "violations": violations,
    }


def _comparison_rows(
    frozen_predictions_path: Path,
    workload_ids: set[str],
) -> list[dict[str, Any]]:
    retained = {
        "v1_frozen_baseline",
        "v2_leave_one_scenario_out",
        "residual_ranking_leave_one_scenario_out",
        "guarded_residual_nested_leave_one_scenario_out",
    }
    rows: list[dict[str, Any]] = [
        cast(dict[str, Any], row)
        for row in _read_csv(frozen_predictions_path)
        if row["evaluation_scheme"] in retained
    ]
    for scheme in retained:
        scheme_ids = {str(row["workload_id"]) for row in rows if row["evaluation_scheme"] == scheme}
        if scheme_ids != workload_ids:
            raise ValueError(f"Frozen comparison workload mismatch for {scheme}")
    return rows


def _report(summary: dict[str, Any]) -> str:
    schemes = cast(dict[str, dict[str, Any]], summary["schemes"])
    lines = [
        "# Mechanism Mask formula development evaluation",
        "",
        "The operation formula was frozen before this end-to-end development evaluation. ",
        "It is not an independent Phase 2G result.",
        "",
        "| Scheme | Within 3% | Mean regret | P95 regret | Max regret |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in schemes.items():
        lines.append(
            "| {name} | {within:.1%} | {mean:.2f}% | {p95:.2f}% | {maximum:.2f}% |".format(
                name=name,
                within=values["within_tie_rate"],
                mean=values["mean_regret_percent"],
                p95=values["p95_regret_percent"],
                maximum=values["max_regret_percent"],
            )
        )
    lines.extend(
        [
            "",
            f"Development gate passed: **{summary['passes_development_gate']}**.",
            "",
            "Phase 2G remains unauthorized until a passing artifact and its protocol are ",
            "separately frozen in version control.",
        ]
    )
    return "\n".join(lines) + "\n"


def develop_mechanism_mask_optimizer(
    pilot_run_dir: str | Path,
    join_run_dir: str | Path,
    workload_run_dirs: list[str | Path],
    frozen_predictions_path: str | Path,
    output_dir: str | Path,
    *,
    ridge_lambda: float = 0.01,
) -> Path:
    """Run the one-shot development evaluation and save auditable artifacts."""

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    model, component_cv_rows, component_cv = fit_mechanism_mask_model(
        pilot_run_dir, join_run_dir, ridge_lambda=ridge_lambda
    )
    observations = load_mask_workload_observations(workload_run_dirs)
    workload_ids = {item.workload_id for item in observations}
    mechanism_rows = [_prediction_row(item, model) for item in observations]
    comparison_rows = _comparison_rows(Path(frozen_predictions_path), workload_ids)
    all_rows = comparison_rows + mechanism_rows
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        grouped.setdefault(str(row["evaluation_scheme"]), []).append(row)
    schemes = {name: _scheme_summary(rows) for name, rows in sorted(grouped.items())}
    baseline = schemes["v1_frozen_baseline"]
    primary = schemes["mechanism_formula_fixed_development"]
    monotonicity = audit_mechanism_monotonicity(
        model,
        row_counts=tuple(sorted({item.features.join_input_rows for item in observations})),
        identifier_widths=tuple(
            sorted({item.features.identifier_width_bytes for item in observations})
        ),
    )
    governance = audit_governance_hard_constraints(model)
    gate_checks = {
        "within_tie_rate_strictly_improves_v1": (
            primary["within_tie_rate"] > baseline["within_tie_rate"]
        ),
        "mean_regret_does_not_worsen_v1": (
            primary["mean_regret_percent"] <= baseline["mean_regret_percent"] + 1e-9
        ),
        "p95_regret_does_not_worsen_v1": (
            primary["p95_regret_percent"] <= baseline["p95_regret_percent"] + 1e-9
        ),
        "max_regret_does_not_worsen_v1": (
            primary["max_regret_percent"] <= baseline["max_regret_percent"] + 1e-9
        ),
        "match_rate_monotonicity_passes": monotonicity["passes"],
        "governance_hard_constraints_pass": governance["passes"],
    }
    passes = all(gate_checks.values())
    summary: dict[str, Any] = {
        "evaluation_label": "mechanism_formula_development_not_phase2g",
        "formula_frozen_before_end_to_end_evaluation": True,
        "observation_count": len(observations),
        "component_cross_validation": component_cv,
        "schemes": schemes,
        "monotonicity_audit": monotonicity,
        "governance_audit": governance,
        "gate_policy": {
            "baseline": "v1_frozen_baseline",
            "primary": "mechanism_formula_fixed_development",
            "checks": gate_checks,
        },
        "passes_development_gate": passes,
        "status": (
            "eligible_for_separate_freeze_before_phase2g"
            if passes
            else "development_only_rejected_by_predeclared_gate"
        ),
        "phase2g_authorized": False,
        "scientific_boundary": (
            "Microbenchmarks fit component costs; end-to-end winners evaluate but do "
            "not fit the formula. Phase 2G remains untouched."
        ),
    }
    _write_json(output / "mechanism_mask_cost_model.json", model.to_dict())
    _write_csv(output / "component_cross_validation.csv", component_cv_rows)
    _write_csv(output / "end_to_end_predictions.csv", all_rows)
    _write_json(output / "summary.json", summary)
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    return output
