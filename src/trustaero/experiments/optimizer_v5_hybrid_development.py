"""Leakage-safe development runner for the V5 hybrid Mask cost model.

The mechanism model is a frozen prior learned from isolated operator
microbenchmarks.  This module fits only the missing complete-pipeline residual
on seed-aggregated workload families, and leaves one complete family out per
fold.  No real-data or external-holdout labels are training targets here.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.pipeline_optimizer import (
    PipelineMaskFamilyObservation,
    load_pipeline_mask_families,
)
from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_data_pilot import _git_state
from trustaero.optimizer.mask import (
    MaskPlacement,
    choose_mask_placement,
)
from trustaero.optimizer.mask_mechanism import MechanismMaskCostModel
from trustaero.optimizer.mask_pipeline_v5 import (
    PipelineV5HybridModel,
    PipelineV5ResidualSurface,
    choose_mask_placement_v5,
    v5_residual_feature_vector,
    v5_support_feature_vector,
)
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class V5SourceBinding:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class V5DevelopmentGates:
    minimum_direct_coverage: float
    minimum_within_tie_improvement_over_v1: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_max_regret_percent: float


@dataclass(frozen=True, slots=True)
class V5HybridDevelopmentConfig:
    protocol_name: str
    results_dir: str
    source_run_dirs: tuple[str, ...]
    source_bindings: tuple[V5SourceBinding, ...]
    mechanism_model_path: str
    mechanism_model_sha256: str
    training_readiness_record_path: str
    training_readiness_record_sha256: str
    ridge_lambda: float
    uncertainty_multiplier: float
    tie_threshold_fraction: float
    require_clean_git: bool
    gates: V5DevelopmentGates
    scientific_boundary: str


def load_v5_hybrid_development_config(
    path: Path | str,
) -> V5HybridDevelopmentConfig:
    """Load the frozen V5 development protocol."""

    payload = _load_json(Path(path))
    gates = cast(dict[str, Any], payload["gates"])
    return V5HybridDevelopmentConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        source_run_dirs=tuple(str(item) for item in payload["source_run_dirs"]),
        source_bindings=tuple(
            V5SourceBinding(path=str(item["path"]), sha256=str(item["sha256"]))
            for item in cast(list[dict[str, Any]], payload["source_bindings"])
        ),
        mechanism_model_path=str(payload["mechanism_model_path"]),
        mechanism_model_sha256=str(payload["mechanism_model_sha256"]),
        training_readiness_record_path=str(payload["training_readiness_record_path"]),
        training_readiness_record_sha256=str(payload["training_readiness_record_sha256"]),
        ridge_lambda=float(payload["ridge_lambda"]),
        uncertainty_multiplier=float(payload["uncertainty_multiplier"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
        gates=V5DevelopmentGates(
            minimum_direct_coverage=float(gates["minimum_direct_coverage"]),
            minimum_within_tie_improvement_over_v1=float(
                gates["minimum_within_tie_improvement_over_v1"]
            ),
            maximum_mean_regret_percent=float(gates["maximum_mean_regret_percent"]),
            maximum_p95_regret_percent=float(gates["maximum_p95_regret_percent"]),
            maximum_max_regret_percent=float(gates["maximum_max_regret_percent"]),
        ),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _scalers(
    vectors: list[tuple[float, ...]],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    columns = list(zip(*vectors, strict=True))
    means = tuple(statistics.mean(column) for column in columns)
    scales = tuple(
        statistics.pstdev(column) if statistics.pstdev(column) > 1e-12 else 1.0
        for column in columns
    )
    return means, scales


def fit_v5_residual_surface(
    observations: list[PipelineMaskFamilyObservation],
    mechanism_prior: MechanismMaskCostModel,
    *,
    ridge_lambda: float,
    uncertainty_multiplier: float,
    max_iterations: int = 20_000,
    tolerance: float = 1e-10,
) -> PipelineV5ResidualSurface:
    """Fit candidate residuals over a positive mechanism-cost offset."""

    feature_count = 6
    if len(observations) <= feature_count + 1:
        raise ValueError("V5 fitting needs more complete families than features")
    if ridge_lambda <= 0.0 or uncertainty_multiplier < 0.0:
        raise ValueError("V5 fitting parameters are invalid")
    vectors: list[tuple[float, ...]] = []
    targets: list[float] = []
    for item in observations:
        for placement, latency in (
            (MaskPlacement.EARLY, item.median_early_latency_ms),
            (MaskPlacement.LATE, item.median_late_latency_ms),
        ):
            prior_ms = mechanism_prior.predict_candidate_ms(item.features, placement)
            if prior_ms <= 0.0 or latency <= 0.0:
                raise ValueError("V5 costs and targets must be positive")
            vectors.append(
                v5_residual_feature_vector(
                    item.features,
                    placement,
                    hashed_identifier_width_bytes=(mechanism_prior.hashed_identifier_width_bytes),
                )
            )
            targets.append(math.log(latency) - math.log(prior_ms))
    means, scales = _scalers(vectors)
    standardized = [
        tuple(
            (value - mean) / scale for value, mean, scale in zip(vector, means, scales, strict=True)
        )
        for vector in vectors
    ]
    coefficients = [0.0] * feature_count
    intercept = statistics.mean(targets)
    for _iteration in range(max_iterations):
        largest_change = 0.0
        updated_intercept = statistics.mean(
            target
            - sum(
                coefficient * value for coefficient, value in zip(coefficients, vector, strict=True)
            )
            for vector, target in zip(standardized, targets, strict=True)
        )
        largest_change = max(largest_change, abs(updated_intercept - intercept))
        intercept = updated_intercept
        for index in range(feature_count):
            numerator = 0.0
            denominator = ridge_lambda
            for vector, target in zip(standardized, targets, strict=True):
                residual_without_current = (
                    target
                    - intercept
                    - sum(
                        coefficient * value
                        for other, (coefficient, value) in enumerate(
                            zip(coefficients, vector, strict=True)
                        )
                        if other != index
                    )
                )
                numerator += vector[index] * residual_without_current
                denominator += vector[index] ** 2
            updated = numerator / denominator
            largest_change = max(largest_change, abs(updated - coefficients[index]))
            coefficients[index] = updated
        if largest_change <= tolerance:
            break
    support_vectors = [v5_support_feature_vector(item.features) for item in observations]
    support_columns = list(zip(*support_vectors, strict=True))
    provisional = PipelineV5ResidualSurface(
        intercept_log_ms=intercept,
        coefficients=tuple(coefficients),
        feature_means=means,
        feature_scales=scales,
        support_minima=tuple(min(column) for column in support_columns),
        support_maxima=tuple(max(column) for column in support_columns),
        residual_log_ratio_rmse=0.0,
        uncertainty_multiplier=uncertainty_multiplier,
        ridge_lambda=ridge_lambda,
        training_family_count=len(observations),
        source_run_ids=tuple(
            sorted({run_id for item in observations for run_id in item.source_run_ids})
        ),
    )
    model = PipelineV5HybridModel(mechanism_prior, provisional)
    errors = [
        model.predict_log_early_late_ratio(item.features) - item.observed_log_early_late_ratio
        for item in observations
    ]
    rmse = math.sqrt(statistics.mean(error**2 for error in errors))
    return PipelineV5ResidualSurface(
        intercept_log_ms=provisional.intercept_log_ms,
        coefficients=provisional.coefficients,
        feature_means=provisional.feature_means,
        feature_scales=provisional.feature_scales,
        support_minima=provisional.support_minima,
        support_maxima=provisional.support_maxima,
        residual_log_ratio_rmse=rmse,
        uncertainty_multiplier=uncertainty_multiplier,
        ridge_lambda=ridge_lambda,
        training_family_count=len(observations),
        source_run_ids=provisional.source_run_ids,
    )


def _regret_percent(observed_log_ratio: float, placement: MaskPlacement) -> float:
    ratio = (
        max(1.0, math.exp(observed_log_ratio))
        if placement is MaskPlacement.EARLY
        else max(1.0, math.exp(-observed_log_ratio))
    )
    return (ratio - 1.0) * 100.0


def evaluate_v5_fold(
    held_out: PipelineMaskFamilyObservation,
    training: list[PipelineMaskFamilyObservation],
    mechanism_prior: MechanismMaskCostModel,
    *,
    ridge_lambda: float,
    uncertainty_multiplier: float,
) -> list[dict[str, object]]:
    """Fit one family-safe fold and report V1, direct V5, and guarded V5."""

    surface = fit_v5_residual_surface(
        training,
        mechanism_prior,
        ridge_lambda=ridge_lambda,
        uncertainty_multiplier=uncertainty_multiplier,
    )
    model = PipelineV5HybridModel(mechanism_prior, surface)
    prediction = model.predict_log_early_late_ratio(held_out.features)
    direct = MaskPlacement.EARLY if prediction < 0.0 else MaskPlacement.LATE
    guarded = choose_mask_placement_v5(held_out.features, model)
    v1 = choose_mask_placement(held_out.features).placement
    actual = held_out.observed_log_early_late_ratio
    oracle = MaskPlacement.EARLY if actual < 0.0 else MaskPlacement.LATE
    rows: list[dict[str, object]] = []
    for scheme, placement, direct_credit, reason in (
        ("v1", v1, True, "FROZEN_V1"),
        ("v5_direct", direct, True, "V5_RAW_COMPONENT_RANKING"),
        (
            "v5_guarded",
            guarded.placement,
            guarded.direct_cost_decision,
            guarded.reason_code,
        ),
    ):
        regret = _regret_percent(actual, placement)
        rows.append(
            {
                "family_id": held_out.family_id,
                "scheme": scheme,
                "seed_count": held_out.seed_count,
                "join_input_rows": held_out.features.join_input_rows,
                "identifier_width_bytes": held_out.features.identifier_width_bytes,
                "join_match_rate": held_out.features.join_match_rate,
                "observed_log_early_late_ratio": actual,
                "predicted_log_early_late_ratio": prediction,
                "selected_placement": placement.value,
                "oracle_placement": oracle.value,
                "exact_top1": placement is oracle,
                "within_tie_threshold": (regret <= held_out.tie_threshold_fraction * 100.0),
                "regret_percent": regret,
                "direct_cost_decision": direct_credit,
                "reason_code": reason,
                "within_training_support": surface.is_within_support(held_out.features),
                "uncertainty_margin": surface.uncertainty_margin,
            }
        )
    return rows


def summarize_v5_predictions(
    rows: list[dict[str, object]],
    *,
    gates: V5DevelopmentGates,
) -> dict[str, object]:
    """Apply predeclared metrics and gates without refitting."""

    def metrics(scheme: str) -> dict[str, float]:
        selected = [row for row in rows if row["scheme"] == scheme]
        regrets = [float(cast(Any, row["regret_percent"])) for row in selected]
        ordered = sorted(regrets)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        return {
            "unit_count": float(len(selected)),
            "exact_top1_rate": statistics.mean(float(bool(row["exact_top1"])) for row in selected),
            "within_tie_rate": statistics.mean(
                float(bool(row["within_tie_threshold"])) for row in selected
            ),
            "mean_regret_percent": statistics.mean(regrets),
            "p95_regret_percent": p95,
            "max_regret_percent": max(regrets),
            "direct_coverage": statistics.mean(
                float(bool(row["direct_cost_decision"])) for row in selected
            ),
        }

    v1 = metrics("v1")
    direct = metrics("v5_direct")
    guarded = metrics("v5_guarded")
    checks = {
        "minimum_direct_coverage": (guarded["direct_coverage"] >= gates.minimum_direct_coverage),
        "minimum_within_tie_improvement_over_v1": (
            guarded["within_tie_rate"] - v1["within_tie_rate"]
            >= gates.minimum_within_tie_improvement_over_v1
        ),
        "maximum_mean_regret_percent": (
            guarded["mean_regret_percent"] <= gates.maximum_mean_regret_percent
        ),
        "maximum_p95_regret_percent": (
            guarded["p95_regret_percent"] <= gates.maximum_p95_regret_percent
        ),
        "maximum_max_regret_percent": (
            guarded["max_regret_percent"] <= gates.maximum_max_regret_percent
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "status": (
            "PASS_V5_HYBRID_DEVELOPMENT_GATE"
            if passed
            else "FAIL_V5_HYBRID_DEVELOPMENT_GATE_RETAIN"
        ),
        "v5_model_freeze_authorized": passed,
        "metrics": {"v1": v1, "v5_direct": direct, "v5_guarded": guarded},
        "gate_checks": checks,
        "external_partition_accessed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError("V5 prediction output cannot be empty")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_v5_hybrid_development(
    config: V5HybridDevelopmentConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume family-level CV and write a frozen-style result bundle."""

    root = project_root.resolve()
    bindings = (
        *config.source_bindings,
        V5SourceBinding(config.mechanism_model_path, config.mechanism_model_sha256),
        V5SourceBinding(
            config.training_readiness_record_path,
            config.training_readiness_record_sha256,
        ),
    )
    for binding in bindings:
        if sha256_file(root / binding.path) != binding.sha256:
            raise ValueError(f"V5 development input changed: {binding.path}")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("V5 development requires a clean commit")
    observations = load_pipeline_mask_families(
        [root / item for item in config.source_run_dirs],
        tie_threshold_fraction=config.tie_threshold_fraction,
    )
    prior = MechanismMaskCostModel.from_dict(_load_json(root / config.mechanism_model_path))
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / config.results_dir / run_id
    folds_dir = run_dir / "folds"
    folds_dir.mkdir(parents=True, exist_ok=True)
    config_payload = json.loads(json.dumps(asdict(config), sort_keys=True))
    config_path = run_dir / "config.json"
    if resume_run_id and config_path.is_file():
        if _load_json(config_path) != config_payload:
            raise ValueError("V5 resume config changed")
    _atomic_json(config_path, config_payload)
    _atomic_json(
        run_dir / "environment.json",
        {"commit_hash": commit, "git_dirty": dirty, "python_model": "cpu"},
    )
    if resume_run_id is None:
        _atomic_json(run_dir.parent / "latest_run.json", {"run_id": run_id})
    started = time.perf_counter()
    total = len(observations)
    for index, held_out in enumerate(observations, start=1):
        fold_path = folds_dir / f"{held_out.family_id}.json"
        if not fold_path.is_file():
            training = [item for item in observations if item.family_id != held_out.family_id]
            fold_rows = evaluate_v5_fold(
                held_out,
                training,
                prior,
                ridge_lambda=config.ridge_lambda,
                uncertainty_multiplier=config.uncertainty_multiplier,
            )
            _atomic_json(fold_path, {"family_id": held_out.family_id, "rows": fold_rows})
        if progress_callback is not None:
            progress_callback(index, total, held_out.family_id, time.perf_counter() - started)
    rows = [
        row
        for path in sorted(folds_dir.glob("*.json"))
        for row in cast(list[dict[str, object]], _load_json(path)["rows"])
    ]
    if len(rows) != total * 3:
        raise ValueError("V5 cross-validation result is incomplete")
    summary = summarize_v5_predictions(rows, gates=config.gates)
    full_surface = fit_v5_residual_surface(
        observations,
        prior,
        ridge_lambda=config.ridge_lambda,
        uncertainty_multiplier=config.uncertainty_multiplier,
    )
    full_model = PipelineV5HybridModel(prior, full_surface)
    _write_csv(run_dir / "cross_validation.csv", rows)
    _atomic_json(run_dir / "summary.json", summary)
    _atomic_json(run_dir / "pipeline_v5_hybrid_model.json", full_model.to_dict())
    return run_dir
