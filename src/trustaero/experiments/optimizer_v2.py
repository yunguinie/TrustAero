"""Development-only training and grouped cross-validation for Mask V2.

Repeated seeds are aggregated before fitting. Cross-validation holds out an
entire workload or scenario family, so measurements from the same controlled
distribution cannot appear on both sides of a fold.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from trustaero.optimizer.mask import (
    MaskPlacement,
    MaskPlacementFeatures,
    choose_mask_placement,
)
from trustaero.optimizer.mask_v2 import (
    MASK_V2_FEATURE_NAMES,
    MaskV2Model,
    choose_mask_placement_v2,
    mask_v2_feature_vector,
)


@dataclass(frozen=True)
class MaskWorkloadObservation:
    """One independent workload distribution after paired-seed aggregation."""

    workload_id: str
    scenario_group_id: str
    source_run_id: str
    source_commit_hash: str
    scenario_id: str
    row_count: int
    seed_count: int
    features: MaskPlacementFeatures
    observed_log_early_late_ratio: float
    median_early_latency_ms: float
    median_late_latency_ms: float
    tie_threshold_fraction: float


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing Mask optimizer artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _scenario_widths(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("scenarios")
    if not isinstance(raw, list):
        raise ValueError("Mask optimizer source config has no scenarios")
    output: dict[str, int] = {}
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("Mask optimizer scenario must be an object")
        item = cast(dict[str, Any], value)
        output[str(item["scenario_id"])] = int(item["identifier_width"])
    return output


def _unit_choices(rows: list[dict[str, str]]) -> dict[str, dict[MaskPlacement, dict[str, str]]]:
    units: dict[str, dict[MaskPlacement, dict[str, str]]] = {}
    for row in rows:
        placement = (
            MaskPlacement.EARLY
            if int(row["raw_sensitive_rows_exposed_to_join"]) == 0
            else MaskPlacement.LATE
        )
        choices = units.setdefault(row["unit_id"], {})
        if placement in choices:
            raise ValueError(f"Unit {row['unit_id']} has duplicate {placement.value} candidates")
        choices[placement] = row
    for unit_id, choices in units.items():
        if set(choices) != {MaskPlacement.EARLY, MaskPlacement.LATE}:
            raise ValueError(f"Unit {unit_id} lacks one Mask placement")
    return units


def load_mask_workload_observations(
    run_dirs: list[str | Path],
) -> list[MaskWorkloadObservation]:
    """Load complete runs and aggregate paired seed ratios per workload."""

    observations: list[MaskWorkloadObservation] = []
    for run_value in run_dirs:
        run_dir = Path(run_value).resolve()
        summary = _read_object(run_dir / "summary.json")
        if summary.get("status") != "complete" or summary.get("all_results_equivalent") is not True:
            raise ValueError(f"Source run is incomplete or non-equivalent: {run_dir}")
        run_id = str(summary.get("run_id", run_dir.name))
        tie_threshold = float(summary["tie_threshold_fraction"])
        widths = _scenario_widths(_read_object(run_dir / "config.json"))
        environment = _read_object(run_dir / "environment.json")
        source_commit = str(environment.get("commit_hash", "unknown"))
        grouped: dict[tuple[str, int], list[dict[MaskPlacement, dict[str, str]]]] = {}
        for choices in _unit_choices(_read_csv(run_dir / "strategy_summary.csv")).values():
            late = choices[MaskPlacement.LATE]
            key = (late["scenario_id"], int(late["row_count"]))
            grouped.setdefault(key, []).append(choices)
        for (scenario_id, row_count), seeds in sorted(grouped.items()):
            logs: list[float] = []
            early_latencies: list[float] = []
            late_latencies: list[float] = []
            input_rows: list[int] = []
            match_rates: list[float] = []
            for choices in seeds:
                early = choices[MaskPlacement.EARLY]
                late = choices[MaskPlacement.LATE]
                early_ms = float(early["median_governed_latency_ms"])
                late_ms = float(late["median_governed_latency_ms"])
                if early_ms <= 0.0 or late_ms <= 0.0:
                    raise ValueError("Mask optimizer latencies must be positive")
                join_input = int(late["after_policy_rows"])
                join_output = int(late["after_join_rows"])
                early_latencies.append(early_ms)
                late_latencies.append(late_ms)
                logs.append(math.log(early_ms / late_ms))
                input_rows.append(join_input)
                match_rates.append(join_output / join_input if join_input else 0.0)
            features = MaskPlacementFeatures(
                join_input_rows=round(statistics.median(input_rows)),
                identifier_width_bytes=widths[scenario_id],
                join_match_rate=statistics.median(match_rates),
            )
            scenario_group = f"{run_id}/{scenario_id}"
            observations.append(
                MaskWorkloadObservation(
                    workload_id=f"{scenario_group}/n{row_count}",
                    scenario_group_id=scenario_group,
                    source_run_id=run_id,
                    source_commit_hash=source_commit,
                    scenario_id=scenario_id,
                    row_count=row_count,
                    seed_count=len(seeds),
                    features=features,
                    observed_log_early_late_ratio=statistics.median(logs),
                    median_early_latency_ms=statistics.median(early_latencies),
                    median_late_latency_ms=statistics.median(late_latencies),
                    tie_threshold_fraction=tie_threshold,
                )
            )
    if not observations:
        raise ValueError("No Mask workload observations were loaded")
    return observations


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense system using partial-pivot Gaussian elimination."""

    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1e-12:
            raise ValueError("Mask V2 regression system is singular")
        augmented[column] = [value / divisor for value in augmented[column]]
        for row_index in range(size):
            if row_index == column:
                continue
            multiplier = augmented[row_index][column]
            augmented[row_index] = [
                value - multiplier * pivot_value
                for value, pivot_value in zip(
                    augmented[row_index], augmented[column], strict=True
                )
            ]
    return [augmented[index][-1] for index in range(size)]


def fit_mask_v2_model(
    observations: list[MaskWorkloadObservation],
    *,
    ridge_lambda: float = 0.01,
) -> MaskV2Model:
    """Fit the frozen feature basis; the intercept is not regularized."""

    feature_count = len(MASK_V2_FEATURE_NAMES)
    if len(observations) <= feature_count:
        raise ValueError(f"Mask V2 needs more than {feature_count} workload observations")
    if ridge_lambda < 0.0:
        raise ValueError("ridge_lambda must be non-negative")
    raw = [mask_v2_feature_vector(item.features) for item in observations]
    means = tuple(statistics.mean(row[index] for row in raw) for index in range(feature_count))
    scales = tuple(
        statistics.pstdev(row[index] for row in raw) or 1.0 for index in range(feature_count)
    )
    design = [
        [1.0]
        + [
            (row[index] - means[index]) / scales[index]
            for index in range(feature_count)
        ]
        for row in raw
    ]
    targets = [item.observed_log_early_late_ratio for item in observations]
    parameter_count = feature_count + 1
    normal_matrix = [
        [
            sum(row[left] * row[right] for row in design)
            + (ridge_lambda if left == right and left > 0 else 0.0)
            for right in range(parameter_count)
        ]
        for left in range(parameter_count)
    ]
    normal_vector = [
        sum(row[index] * target for row, target in zip(design, targets, strict=True))
        for index in range(parameter_count)
    ]
    parameters = _solve_linear_system(normal_matrix, normal_vector)
    return MaskV2Model(
        intercept=parameters[0],
        coefficients=tuple(parameters[1:]),
        feature_means=means,
        feature_scales=scales,
        ridge_lambda=ridge_lambda,
        training_sample_count=len(observations),
    )


def _prediction_row(
    observation: MaskWorkloadObservation,
    *,
    scheme: str,
    holdout_group: str,
    placement: MaskPlacement,
    predicted_log_ratio: float,
) -> dict[str, Any]:
    actual = observation.observed_log_early_late_ratio
    oracle = MaskPlacement.EARLY if actual < 0.0 else MaskPlacement.LATE
    if placement is MaskPlacement.EARLY:
        regret_ratio = max(1.0, math.exp(actual))
        speedup_late = math.exp(-actual)
        speedup_early = 1.0
    else:
        regret_ratio = max(1.0, math.exp(-actual))
        speedup_late = 1.0
        speedup_early = math.exp(actual)
    return {
        "evaluation_scheme": scheme,
        "holdout_group": holdout_group,
        "workload_id": observation.workload_id,
        "scenario_group_id": observation.scenario_group_id,
        "scenario_id": observation.scenario_id,
        "row_count": observation.row_count,
        "seed_count": observation.seed_count,
        "join_input_rows": observation.features.join_input_rows,
        "identifier_width_bytes": observation.features.identifier_width_bytes,
        "join_match_rate": observation.features.join_match_rate,
        "observed_log_early_late_ratio": actual,
        "predicted_log_early_late_ratio": predicted_log_ratio,
        "selected_placement": placement.value,
        "oracle_placement": oracle.value,
        "exact_top1": placement is oracle,
        "within_tie_threshold": regret_ratio - 1.0 <= observation.tie_threshold_fraction,
        "regret_percent": (regret_ratio - 1.0) * 100.0,
        "speedup_vs_fixed_late_ratio": speedup_late,
        "speedup_vs_fixed_early_ratio": speedup_early,
    }


def cross_validate_mask_v2(
    observations: list[MaskWorkloadObservation],
    *,
    split: str,
    ridge_lambda: float = 0.01,
) -> list[dict[str, Any]]:
    """Cross-validate with either workload or scenario-family isolation."""

    if split == "workload":
        scheme = "v2_leave_one_workload_out"
    elif split == "scenario":
        scheme = "v2_leave_one_scenario_out"
    else:
        raise ValueError("split must be 'workload' or 'scenario'")

    def group_key(item: MaskWorkloadObservation) -> str:
        return item.workload_id if split == "workload" else item.scenario_group_id

    groups = sorted({group_key(item) for item in observations})
    output: list[dict[str, Any]] = []
    for group in groups:
        training = [item for item in observations if group_key(item) != group]
        testing = [item for item in observations if group_key(item) == group]
        model = fit_mask_v2_model(training, ridge_lambda=ridge_lambda)
        for item in testing:
            decision = choose_mask_placement_v2(item.features, model)
            output.append(
                _prediction_row(
                    item,
                    scheme=scheme,
                    holdout_group=group,
                    placement=decision.placement,
                    predicted_log_ratio=decision.predicted_log_early_late_ratio,
                )
            )
    return output


def audit_match_rate_monotonicity(
    model: MaskV2Model,
    *,
    row_counts: tuple[int, ...],
    identifier_widths: tuple[int, ...],
    match_rates: tuple[float, ...] = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Check that higher Join match rate never makes early Mask less attractive.

    For fixed rows and width, late Mask hashes more rows as the match rate
    rises, while early Mask already hashes every input row. Therefore the
    predicted log(early/late) ratio should be non-increasing. This is an audit,
    not a post-hoc repair of model predictions.
    """

    ordered_rates = tuple(sorted(set(match_rates)))
    if len(ordered_rates) < 2 or any(not 0.0 <= value <= 1.0 for value in ordered_rates):
        raise ValueError("match_rates must contain at least two values in [0, 1]")
    if not row_counts or not identifier_widths:
        raise ValueError("monotonicity audit grids cannot be empty")
    violations: list[dict[str, Any]] = []
    comparisons = 0
    for row_count in row_counts:
        for width in identifier_widths:
            predictions = [
                model.predict_log_latency_ratio(
                    MaskPlacementFeatures(
                        join_input_rows=row_count,
                        identifier_width_bytes=width,
                        join_match_rate=match_rate,
                    )
                )
                for match_rate in ordered_rates
            ]
            for index in range(1, len(ordered_rates)):
                comparisons += 1
                if predictions[index] > predictions[index - 1] + tolerance:
                    violations.append(
                        {
                            "join_input_rows": row_count,
                            "identifier_width_bytes": width,
                            "lower_match_rate": ordered_rates[index - 1],
                            "higher_match_rate": ordered_rates[index],
                            "lower_prediction": predictions[index - 1],
                            "higher_prediction": predictions[index],
                        }
                    )
    return {
        "comparison_count": comparisons,
        "violation_count": len(violations),
        "passes": not violations,
        "examples": violations[:10],
    }


def _v1_rows(observations: list[MaskWorkloadObservation]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in observations:
        decision = choose_mask_placement(item.features)
        output.append(
            _prediction_row(
                item,
                scheme="v1_frozen_baseline",
                holdout_group="not_applicable",
                placement=decision.placement,
                predicted_log_ratio=math.log(
                    decision.early_proxy_work_bytes / decision.late_proxy_work_bytes
                ),
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
    return {
        "observation_count": len(rows),
        "exact_top1_count": sum(bool(row["exact_top1"]) for row in rows),
        "exact_top1_rate": sum(bool(row["exact_top1"]) for row in rows) / len(rows),
        "within_tie_count": sum(bool(row["within_tie_threshold"]) for row in rows),
        "within_tie_rate": sum(bool(row["within_tie_threshold"]) for row in rows) / len(rows),
        "mean_regret_percent": statistics.mean(regrets),
        "median_regret_percent": statistics.median(regrets),
        "p95_regret_percent": _percentile(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "geometric_speedup_vs_fixed_late_ratio": _geometric_mean(
            [float(row["speedup_vs_fixed_late_ratio"]) for row in rows]
        ),
        "geometric_speedup_vs_fixed_early_ratio": _geometric_mean(
            [float(row["speedup_vs_fixed_early_ratio"]) for row in rows]
        ),
    }


def _observation_rows(observations: list[MaskWorkloadObservation]) -> list[dict[str, Any]]:
    return [
        {
            "workload_id": item.workload_id,
            "scenario_group_id": item.scenario_group_id,
            "source_run_id": item.source_run_id,
            "source_commit_hash": item.source_commit_hash,
            "scenario_id": item.scenario_id,
            "row_count": item.row_count,
            "seed_count": item.seed_count,
            "join_input_rows": item.features.join_input_rows,
            "identifier_width_bytes": item.features.identifier_width_bytes,
            "join_match_rate": item.features.join_match_rate,
            "observed_log_early_late_ratio": item.observed_log_early_late_ratio,
            "median_early_latency_ms": item.median_early_latency_ms,
            "median_late_latency_ms": item.median_late_latency_ms,
            "tie_threshold_fraction": item.tie_threshold_fraction,
        }
        for item in observations
    ]


def _report(summary: dict[str, Any]) -> str:
    schemes = cast(dict[str, dict[str, Any]], summary["schemes"])
    lines = [
        "# Mask Optimizer V2 development report",
        "",
        "This is grouped development cross-validation, not an independent held-out result.",
        "",
        "| Scheme | Exact top-1 | Within 3% | Mean regret | P95 regret | vs late | vs early |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in schemes.items():
        lines.append(
            "| {name} | {exact:.1%} | {within:.1%} | {mean:.2f}% | {p95:.2f}% | "
            "{late:.3f}x | {early:.3f}x |".format(
                name=name,
                exact=values["exact_top1_rate"],
                within=values["within_tie_rate"],
                mean=values["mean_regret_percent"],
                p95=values["p95_regret_percent"],
                late=values["geometric_speedup_vs_fixed_late_ratio"],
                early=values["geometric_speedup_vs_fixed_early_ratio"],
            )
        )
    lines.extend(
        [
            "",
            "> The feature basis and ridge constant were developed after inspecting Phase 2E/F. ",
            "> Only a newly frozen workload can measure V2 generalization.",
        ]
    )
    return "\n".join(lines) + "\n"


def develop_mask_optimizer_v2(
    run_dirs: list[str | Path],
    output_dir: str | Path,
    *,
    ridge_lambda: float = 0.01,
) -> Path:
    """Fit V2, run two leakage-resistant CV schemes, and save artifacts."""

    observations = load_mask_workload_observations(run_dirs)
    predictions = _v1_rows(observations)
    predictions.extend(
        cross_validate_mask_v2(observations, split="workload", ridge_lambda=ridge_lambda)
    )
    predictions.extend(
        cross_validate_mask_v2(observations, split="scenario", ridge_lambda=ridge_lambda)
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in predictions:
        grouped.setdefault(str(row["evaluation_scheme"]), []).append(row)
    schemes = {name: _scheme_summary(rows) for name, rows in sorted(grouped.items())}
    final_model = fit_mask_v2_model(observations, ridge_lambda=ridge_lambda)
    monotonicity = audit_match_rate_monotonicity(
        final_model,
        row_counts=tuple(sorted({item.features.join_input_rows for item in observations})),
        identifier_widths=tuple(
            sorted({item.features.identifier_width_bytes for item in observations})
        ),
    )
    summary: dict[str, Any] = {
        "evaluation_label": "development_cross_validation",
        "observation_count": len(observations),
        "source_run_ids": sorted({item.source_run_id for item in observations}),
        "source_commit_hashes": sorted({item.source_commit_hash for item in observations}),
        "feature_names": list(MASK_V2_FEATURE_NAMES),
        "ridge_lambda": ridge_lambda,
        "schemes": schemes,
        "match_rate_monotonicity_audit": monotonicity,
        "limitations": [
            "Phase 2F is development data for V2 after being a valid held-out test for V1.",
            "Feature-basis development inspected Phase 2E/F, so CV is descriptive, not final.",
            "Cardinality features use realized controlled statistics; estimator error is untested.",
            "Only synthetic workloads, two Mask placements, one machine, and one DBMS are covered.",
        ],
    }
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "observations.csv", _observation_rows(observations))
    _write_csv(output / "cross_validation_predictions.csv", predictions)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    model_payload = final_model.to_dict()
    model_payload["training_source_run_ids"] = summary["source_run_ids"]
    model_payload["status"] = "development_only_not_held_out_validated"
    (output / "model.json").write_text(
        json.dumps(model_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    return output
