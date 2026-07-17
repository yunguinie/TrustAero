"""Leakage-safe development of the complete-fragment Mask cost model."""

from __future__ import annotations

import csv
import json
import math
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from trustaero.optimizer.mask import (
    MaskPlacement,
    MaskPlacementFeatures,
    choose_mask_placement,
)
from trustaero.optimizer.mask_pipeline import (
    PIPELINE_COST_FEATURE_NAMES,
    PIPELINE_SUPPORT_FEATURE_NAMES,
    PipelineMaskCostModel,
    choose_mask_placement_by_pipeline_cost,
    pipeline_cost_feature_vector,
    pipeline_support_feature_vector,
)


@dataclass(frozen=True)
class PipelineMaskFamilyObservation:
    """One physical workload family after all paired seeds are aggregated."""

    family_id: str
    source_run_ids: tuple[str, ...]
    source_commit_hashes: tuple[str, ...]
    seed_count: int
    features: MaskPlacementFeatures
    median_early_latency_ms: float
    median_late_latency_ms: float
    observed_log_early_late_ratio: float
    tie_threshold_fraction: float


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing pipeline optimizer artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_pipeline_mask_families(
    run_dirs: list[str | Path],
    *,
    tie_threshold_fraction: float = 0.03,
) -> list[PipelineMaskFamilyObservation]:
    """Load complete fragment runs without splitting seeds across families."""

    if not 0.0 <= tie_threshold_fraction < 1.0:
        raise ValueError("tie_threshold_fraction must be in [0, 1)")
    grouped: dict[
        tuple[int, int, float],
        list[tuple[str, str, int, float, float]],
    ] = {}
    for run_value in run_dirs:
        run_dir = Path(run_value).resolve()
        summary = _read_object(run_dir / "summary.json")
        unit_count = int(summary.get("unit_count", -1))
        if (
            summary.get("status") != "complete"
            or summary.get("all_validations_passed") is not True
            or int(summary.get("result_equivalent_fragment_count", -2)) != unit_count
            or int(summary.get("distinct_physical_plan_fragment_count", -3))
            != unit_count
        ):
            raise ValueError(f"Fragment source run is incomplete or invalid: {run_dir}")
        run_id = str(summary["run_id"])
        environment = _read_object(run_dir / "environment.json")
        commit_hash = str(environment.get("commit_hash", "unknown"))
        units: dict[str, dict[str, dict[str, str]]] = {}
        for row in _read_csv(run_dir / "component_summary.csv"):
            if row["benchmark"] != "mask_fragment":
                continue
            components = units.setdefault(row["unit_id"], {})
            component = row["component"]
            if component in components:
                raise ValueError(f"Duplicate fragment component in {row['unit_id']}")
            components[component] = row
        expected = {"early_mask_fragment", "late_mask_fragment"}
        if len(units) != unit_count:
            raise ValueError("Fragment component summary does not cover every unit")
        for unit_id, components in units.items():
            if set(components) != expected:
                raise ValueError(f"Fragment unit lacks a paired candidate: {unit_id}")
            early = components["early_mask_fragment"]
            late = components["late_mask_fragment"]
            key = (
                int(early["row_count"]),
                int(early["identifier_width"]),
                float(early["match_rate"]),
            )
            if any(
                components[name][field] != early[field]
                for name in expected
                for field in (
                    "row_count",
                    "identifier_width",
                    "match_rate",
                    "seed",
                )
            ):
                raise ValueError(f"Fragment pair metadata differs in {unit_id}")
            early_ms = float(early["median_latency_ms"])
            late_ms = float(late["median_latency_ms"])
            if early_ms <= 0.0 or late_ms <= 0.0:
                raise ValueError("Fragment latency must be positive")
            grouped.setdefault(key, []).append(
                (run_id, commit_hash, int(early["seed"]), early_ms, late_ms)
            )
    output: list[PipelineMaskFamilyObservation] = []
    for (rows, width, match_rate), seeds in sorted(grouped.items()):
        # Run ID is part of the replicate identity because separate frozen runs
        # may intentionally reuse a numeric seed at a different protocol stage.
        replicate_ids = {(item[0], item[2]) for item in seeds}
        if len(replicate_ids) != len(seeds):
            raise ValueError("A physical family contains a duplicate run/seed replicate")
        early_values = [item[3] for item in seeds]
        late_values = [item[4] for item in seeds]
        paired_logs = [
            math.log(early / late)
            for early, late in zip(early_values, late_values, strict=True)
        ]
        output.append(
            PipelineMaskFamilyObservation(
                family_id=f"n{rows}-w{width}-m{round(match_rate * 1000):04d}",
                source_run_ids=tuple(sorted({item[0] for item in seeds})),
                source_commit_hashes=tuple(sorted({item[1] for item in seeds})),
                seed_count=len(seeds),
                features=MaskPlacementFeatures(
                    join_input_rows=rows,
                    identifier_width_bytes=width,
                    join_match_rate=match_rate,
                ),
                median_early_latency_ms=statistics.median(early_values),
                median_late_latency_ms=statistics.median(late_values),
                observed_log_early_late_ratio=statistics.median(paired_logs),
                tie_threshold_fraction=tie_threshold_fraction,
            )
        )
    if len(output) <= len(PIPELINE_COST_FEATURE_NAMES) + 1:
        raise ValueError("Pipeline development needs more independent families")
    return output


def _feature_scalers(
    vectors: list[tuple[float, ...]],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    columns = list(zip(*vectors, strict=True))
    means = tuple(statistics.mean(column) for column in columns)
    scales = tuple(
        statistics.pstdev(column) if statistics.pstdev(column) > 1e-12 else 1.0
        for column in columns
    )
    return means, scales


def fit_pipeline_mask_cost_model(
    observations: list[PipelineMaskFamilyObservation],
    *,
    ridge_lambda: float = 0.1,
    uncertainty_multiplier: float = 1.0,
    max_iterations: int = 20_000,
    tolerance: float = 1e-10,
) -> PipelineMaskCostModel:
    """Fit one shared non-negative formula to both physical candidates."""

    if len(observations) <= len(PIPELINE_COST_FEATURE_NAMES) + 1:
        raise ValueError("Pipeline fitting lacks independent workload families")
    if (
        ridge_lambda < 0.0
        or uncertainty_multiplier < 0.0
        or max_iterations <= 0
        or tolerance <= 0.0
    ):
        raise ValueError("Pipeline fitting parameters are invalid")
    vectors: list[tuple[float, ...]] = []
    targets: list[float] = []
    for item in observations:
        for placement, latency in (
            (MaskPlacement.EARLY, item.median_early_latency_ms),
            (MaskPlacement.LATE, item.median_late_latency_ms),
        ):
            vectors.append(pipeline_cost_feature_vector(item.features, placement))
            targets.append(math.log(latency))
    means, scales = _feature_scalers(vectors)
    standardized = [
        tuple(
            (value - mean) / scale
            for value, mean, scale in zip(vector, means, scales, strict=True)
        )
        for vector in vectors
    ]
    coefficients = [0.0] * len(PIPELINE_COST_FEATURE_NAMES)
    intercept = statistics.mean(targets)
    for _iteration in range(max_iterations):
        largest_change = 0.0
        updated_intercept = statistics.mean(
            target
            - sum(
                coefficient * value
                for coefficient, value in zip(
                    coefficients, vector, strict=True
                )
            )
            for vector, target in zip(standardized, targets, strict=True)
        )
        largest_change = max(largest_change, abs(updated_intercept - intercept))
        intercept = updated_intercept
        for index in range(len(coefficients)):
            numerator = 0.0
            denominator = ridge_lambda
            for vector, target in zip(standardized, targets, strict=True):
                residual_without_current = target - intercept - sum(
                    coefficient * value
                    for other, (coefficient, value) in enumerate(
                        zip(coefficients, vector, strict=True)
                    )
                    if other != index
                )
                numerator += vector[index] * residual_without_current
                denominator += vector[index] ** 2
            updated = max(0.0, numerator / denominator)
            largest_change = max(largest_change, abs(updated - coefficients[index]))
            coefficients[index] = updated
        if largest_change <= tolerance:
            break
    support_vectors = [pipeline_support_feature_vector(item.features) for item in observations]
    support_columns = list(zip(*support_vectors, strict=True))
    provisional = PipelineMaskCostModel(
        intercept_log_ms=intercept,
        coefficients=tuple(coefficients),
        feature_means=means,
        feature_scales=scales,
        support_minima=tuple(min(column) for column in support_columns),
        support_maxima=tuple(max(column) for column in support_columns),
        ridge_lambda=ridge_lambda,
        paired_log_ratio_rmse=0.0,
        uncertainty_multiplier=uncertainty_multiplier,
        training_family_count=len(observations),
        source_run_ids=tuple(
            sorted({run_id for item in observations for run_id in item.source_run_ids})
        ),
    )
    paired_errors = [
        provisional.predict_log_early_late_ratio(item.features)
        - item.observed_log_early_late_ratio
        for item in observations
    ]
    rmse = math.sqrt(statistics.mean(value**2 for value in paired_errors))
    return PipelineMaskCostModel(
        intercept_log_ms=provisional.intercept_log_ms,
        coefficients=provisional.coefficients,
        feature_means=provisional.feature_means,
        feature_scales=provisional.feature_scales,
        support_minima=provisional.support_minima,
        support_maxima=provisional.support_maxima,
        ridge_lambda=ridge_lambda,
        paired_log_ratio_rmse=rmse,
        uncertainty_multiplier=uncertainty_multiplier,
        training_family_count=len(observations),
        source_run_ids=provisional.source_run_ids,
    )


def _prediction_row(
    item: PipelineMaskFamilyObservation,
    *,
    scheme: str,
    placement: MaskPlacement,
    predicted_log_ratio: float,
    used_fallback: bool = False,
    reason_code: str = "",
) -> dict[str, Any]:
    actual = item.observed_log_early_late_ratio
    oracle = MaskPlacement.EARLY if actual < 0.0 else MaskPlacement.LATE
    regret_ratio = (
        max(1.0, math.exp(actual))
        if placement is MaskPlacement.EARLY
        else max(1.0, math.exp(-actual))
    )
    return {
        "evaluation_scheme": scheme,
        "holdout_family_id": item.family_id,
        "family_id": item.family_id,
        "seed_count": item.seed_count,
        "join_input_rows": item.features.join_input_rows,
        "identifier_width_bytes": item.features.identifier_width_bytes,
        "join_match_rate": item.features.join_match_rate,
        "observed_log_early_late_ratio": actual,
        "predicted_log_early_late_ratio": predicted_log_ratio,
        "selected_placement": placement.value,
        "oracle_placement": oracle.value,
        "exact_top1": placement is oracle,
        "within_tie_threshold": regret_ratio - 1.0 <= item.tie_threshold_fraction,
        "regret_percent": (regret_ratio - 1.0) * 100.0,
        "speedup_vs_fixed_late_ratio": (
            math.exp(-actual) if placement is MaskPlacement.EARLY else 1.0
        ),
        "speedup_vs_fixed_early_ratio": (
            1.0 if placement is MaskPlacement.EARLY else math.exp(actual)
        ),
        "used_fallback": used_fallback,
        "reason_code": reason_code,
    }


def cross_validate_pipeline_mask_cost(
    observations: list[PipelineMaskFamilyObservation],
    *,
    ridge_lambda: float = 0.1,
    uncertainty_multiplier: float = 1.0,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> list[dict[str, Any]]:
    """Leave one complete physical family out; paired seeds never cross folds."""

    output: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index, held_out in enumerate(observations, start=1):
        training = [item for item in observations if item.family_id != held_out.family_id]
        model = fit_pipeline_mask_cost_model(
            training,
            ridge_lambda=ridge_lambda,
            uncertainty_multiplier=uncertainty_multiplier,
        )
        decision = choose_mask_placement_by_pipeline_cost(held_out.features, model)
        output.append(
            _prediction_row(
                held_out,
                scheme="pipeline_cost_guarded_leave_one_family_out",
                placement=decision.placement,
                predicted_log_ratio=decision.predicted_log_early_late_ratio,
                used_fallback=decision.used_fallback,
                reason_code=decision.reason_code,
            )
        )
        output.append(
            _prediction_row(
                held_out,
                scheme="pipeline_cost_direct_leave_one_family_out",
                placement=decision.model_placement,
                predicted_log_ratio=decision.predicted_log_early_late_ratio,
                reason_code="PIPELINE_DIRECT_DIAGNOSTIC_ONLY",
            )
        )
        if progress_callback is not None:
            progress_callback(
                index,
                len(observations),
                held_out.family_id,
                time.perf_counter() - started,
            )
    return output


def _baseline_rows(
    observations: list[PipelineMaskFamilyObservation],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in observations:
        v1 = choose_mask_placement(item.features)
        for scheme, placement in (
            ("fixed_early", MaskPlacement.EARLY),
            ("fixed_late", MaskPlacement.LATE),
            ("v1_frozen_baseline", v1.placement),
            (
                "oracle_experimental_upper_bound",
                MaskPlacement.EARLY
                if item.observed_log_early_late_ratio < 0.0
                else MaskPlacement.LATE,
            ),
        ):
            output.append(
                _prediction_row(
                    item,
                    scheme=scheme,
                    placement=placement,
                    predicted_log_ratio=(
                        item.observed_log_early_late_ratio
                        if scheme == "oracle_experimental_upper_bound"
                        else float("nan")
                    ),
                    reason_code=(v1.reason_code if scheme == "v1_frozen_baseline" else ""),
                )
            )
    return output


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _geometric_mean(values: list[float]) -> float:
    return math.exp(statistics.mean(math.log(value) for value in values))


def _scheme_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    regrets = [float(row["regret_percent"]) for row in rows]
    direct_count = sum(not bool(row["used_fallback"]) for row in rows)
    return {
        "family_count": len(rows),
        "exact_top1_rate": sum(bool(row["exact_top1"]) for row in rows) / len(rows),
        "within_tie_rate": sum(bool(row["within_tie_threshold"]) for row in rows)
        / len(rows),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _percentile(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "geometric_speedup_vs_fixed_late_ratio": _geometric_mean(
            [float(row["speedup_vs_fixed_late_ratio"]) for row in rows]
        ),
        "geometric_speedup_vs_fixed_early_ratio": _geometric_mean(
            [float(row["speedup_vs_fixed_early_ratio"]) for row in rows]
        ),
        "direct_model_decision_count": direct_count,
        "direct_model_coverage": direct_count / len(rows),
    }


def _legality_audit(model: PipelineMaskCostModel) -> dict[str, bool]:
    exposure = choose_mask_placement_by_pipeline_cost(
        MaskPlacementFeatures(
            join_input_rows=100_000,
            identifier_width_bytes=256,
            join_match_rate=1.0,
            max_raw_exposure_rows=0,
        ),
        model,
    )
    early_illegal = choose_mask_placement_by_pipeline_cost(
        MaskPlacementFeatures(
            join_input_rows=100_000,
            identifier_width_bytes=256,
            join_match_rate=1.0,
            early_mask_legal=False,
        ),
        model,
    )
    fail_closed = False
    try:
        choose_mask_placement_by_pipeline_cost(
            MaskPlacementFeatures(
                join_input_rows=100_000,
                identifier_width_bytes=256,
                join_match_rate=1.0,
                early_mask_legal=False,
                late_mask_legal=False,
            ),
            model,
        )
    except ValueError:
        fail_closed = True
    return {
        "raw_exposure_limit_forces_early": exposure.placement is MaskPlacement.EARLY,
        "early_illegal_forces_late": early_illegal.placement is MaskPlacement.LATE,
        "no_legal_candidate_fails_closed": fail_closed,
    }


def _observation_rows(
    observations: list[PipelineMaskFamilyObservation],
) -> list[dict[str, Any]]:
    return [
        {
            "family_id": item.family_id,
            "source_run_ids": "|".join(item.source_run_ids),
            "source_commit_hashes": "|".join(item.source_commit_hashes),
            "seed_count": item.seed_count,
            "join_input_rows": item.features.join_input_rows,
            "identifier_width_bytes": item.features.identifier_width_bytes,
            "join_match_rate": item.features.join_match_rate,
            "median_early_latency_ms": item.median_early_latency_ms,
            "median_late_latency_ms": item.median_late_latency_ms,
            "observed_log_early_late_ratio": item.observed_log_early_late_ratio,
            "tie_threshold_fraction": item.tie_threshold_fraction,
        }
        for item in observations
    ]


def _report(summary: dict[str, Any]) -> str:
    schemes = cast(dict[str, dict[str, Any]], summary["schemes"])
    lines = [
        "# Pipeline-aware Mask optimizer development",
        "",
        "This is grouped development cross-validation, not Phase 2G.",
        "",
        "| Scheme | Top-1 | Within 3% | Mean regret | P95 | Max | Direct coverage |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in schemes.items():
        lines.append(
            "| {name} | {top:.1%} | {within:.1%} | {mean:.2f}% | {p95:.2f}% | "
            "{maximum:.2f}% | {coverage:.1%} |".format(
                name=name,
                top=values["exact_top1_rate"],
                within=values["within_tie_rate"],
                mean=values["mean_regret_percent"],
                p95=values["p95_regret_percent"],
                maximum=values["max_regret_percent"],
                coverage=values["direct_model_coverage"],
            )
        )
    lines.extend(
        [
            "",
            f"Development gate passed: {summary['development_gate']['passes']}",
            "",
            "> The Phase 2I/J families informed this model design. The result is ",
            "> development evidence only; Phase 2G remains untouched and unauthorized.",
        ]
    )
    return "\n".join(lines) + "\n"


def develop_pipeline_mask_optimizer(
    run_dirs: list[str | Path],
    output_dir: str | Path,
    *,
    tie_threshold_fraction: float = 0.03,
    ridge_lambda: float = 0.1,
    uncertainty_multiplier: float = 1.0,
    minimum_direct_coverage: float = 0.25,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run the single predeclared development evaluation and save artifacts."""

    if not 0.0 <= minimum_direct_coverage <= 1.0:
        raise ValueError("minimum_direct_coverage must be in [0, 1]")
    observations = load_pipeline_mask_families(
        run_dirs, tie_threshold_fraction=tie_threshold_fraction
    )
    predictions = _baseline_rows(observations)
    predictions.extend(
        cross_validate_pipeline_mask_cost(
            observations,
            ridge_lambda=ridge_lambda,
            uncertainty_multiplier=uncertainty_multiplier,
            progress_callback=progress_callback,
        )
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in predictions:
        grouped.setdefault(str(row["evaluation_scheme"]), []).append(row)
    schemes = {name: _scheme_summary(rows) for name, rows in sorted(grouped.items())}
    final_model = fit_pipeline_mask_cost_model(
        observations,
        ridge_lambda=ridge_lambda,
        uncertainty_multiplier=uncertainty_multiplier,
    )
    legality = _legality_audit(final_model)
    baseline = schemes["v1_frozen_baseline"]
    primary = schemes["pipeline_cost_guarded_leave_one_family_out"]
    checks = {
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
        "direct_model_coverage_meets_minimum": (
            primary["direct_model_coverage"] >= minimum_direct_coverage
        ),
        "all_cost_coefficients_non_negative": all(
            value >= 0.0 for value in final_model.coefficients
        ),
        "all_governance_audits_pass": all(legality.values()),
    }
    passes = all(checks.values())
    summary: dict[str, Any] = {
        "evaluation_label": "phase2k_pipeline_optimizer_development",
        "family_count": len(observations),
        "replicate_count": sum(item.seed_count for item in observations),
        "source_run_ids": sorted(
            {run_id for item in observations for run_id in item.source_run_ids}
        ),
        "source_commit_hashes": sorted(
            {
                commit
                for item in observations
                for commit in item.source_commit_hashes
            }
        ),
        "cost_feature_names": list(PIPELINE_COST_FEATURE_NAMES),
        "support_feature_names": list(PIPELINE_SUPPORT_FEATURE_NAMES),
        "ridge_lambda": ridge_lambda,
        "uncertainty_multiplier": uncertainty_multiplier,
        "minimum_direct_coverage": minimum_direct_coverage,
        "tie_threshold_fraction": tie_threshold_fraction,
        "schemes": schemes,
        "governance_legality_audit": legality,
        "development_gate": {
            "passes": passes,
            "checks": checks,
            "baseline": "v1_frozen_baseline",
            "primary": "pipeline_cost_guarded_leave_one_family_out",
        },
        "status": (
            "eligible_for_separate_model_freeze_before_phase2g"
            if passes
            else "development_only_rejected_by_predeclared_gate"
        ),
        "phase2g_authorized": False,
        "scientific_boundary": (
            "Phase 2I/J informed the formula and are development data. Complete "
            "families, not seeds, are cross-validation units."
        ),
    }
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "family_observations.csv", _observation_rows(observations))
    _write_csv(output / "cross_validation_predictions.csv", predictions)
    model_payload = final_model.to_dict()
    model_payload["status"] = summary["status"]
    model_payload["development_gate"] = summary["development_gate"]
    _write_json(output / "pipeline_mask_cost_model.json", model_payload)
    _write_json(output / "summary.json", summary)
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    return output
