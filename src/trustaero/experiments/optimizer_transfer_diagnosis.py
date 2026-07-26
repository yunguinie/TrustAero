"""Reproducible root-cause diagnosis for the frozen V3 real-data transfer."""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, cast

from trustaero.optimizer.mask import (
    MaskOptimizerConfig,
    MaskPlacement,
    MaskPlacementFeatures,
    choose_mask_placement,
)
from trustaero.optimizer.mask_interaction import (
    INTERACTION_FEATURE_NAMES,
    InteractionMaskCostModel,
    interaction_feature_vector,
    interaction_support_vector,
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected one JSON object: {path}")
    return cast(dict[str, Any], value)


def _nearest_training_observations(
    training: list[dict[str, str]],
    features: MaskPlacementFeatures,
    *,
    count: int = 5,
) -> list[dict[str, Any]]:
    target = interaction_support_vector(features)
    neighbors: list[tuple[float, dict[str, str]]] = []
    for row in training:
        candidate = MaskPlacementFeatures(
            int(row["join_input_rows"]),
            int(row["identifier_width_bytes"]),
            float(row["join_match_rate"]),
        )
        vector = interaction_support_vector(candidate)
        distance = math.sqrt(
            sum((left - right) ** 2 for left, right in zip(target, vector, strict=True))
        )
        neighbors.append((distance, row))
    return [
        {
            "family_id": row["family_id"],
            "support_distance": distance,
            "join_input_rows": int(row["join_input_rows"]),
            "identifier_width_bytes": int(row["identifier_width_bytes"]),
            "join_match_rate": float(row["join_match_rate"]),
            "observed_log_early_late_ratio": float(row["observed_log_early_late_ratio"]),
        }
        for distance, row in sorted(neighbors, key=lambda item: item[0])[:count]
    ]


def v1_early_required_match_width_product(
    join_input_rows: int,
    config: MaskOptimizerConfig | None = None,
) -> float:
    """Return the minimum ``match_rate * raw_width`` for V1 to choose early."""

    if join_input_rows <= 0:
        raise ValueError("join_input_rows must be positive")
    model = config or MaskOptimizerConfig()
    return (
        model.hashed_identifier_width_bytes
        + model.early_materialization_setup_bytes / join_input_rows
    )


def diagnose_optimizer_v3_transfer(project_root: Path, run_dir: Path) -> dict[str, Any]:
    """Explain the V3 all-late behavior without modifying the frozen model."""

    root = project_root.resolve()
    directory = run_dir.resolve()
    config = _read_object(directory / "config.json")
    audit = _read_object(directory / "paired_stability_audit.json")
    model = InteractionMaskCostModel.from_dict(
        _read_object(root / str(config["primary_model_path"]))
    )
    with (
        root / "results/phase2n_optimizer_v3_development/bounded_interaction_consensus_v3/"
        "family_observations.csv"
    ).open(newline="", encoding="utf-8") as stream:
        training = list(csv.DictReader(stream))
    audited_by_id = {
        str(item["family_id"]): item for item in cast(list[dict[str, Any]], audit["family_audits"])
    }
    families = [_read_object(path) for path in sorted((directory / "families").glob("*.json"))]
    family_diagnostics: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    absolute_contributions: dict[str, list[float]] = {
        name: [] for name in INTERACTION_FEATURE_NAMES
    }
    for family in families:
        family_id = str(family["family_id"])
        audited = cast(dict[str, Any], audited_by_id[family_id])
        features = MaskPlacementFeatures(
            int(family["join_input_rows"]),
            int(family["identifier_width_bytes"]),
            float(family["achieved_join_match_rate"]),
        )
        vector = interaction_feature_vector(features)
        terms = [
            coefficient * ((value - mean) / scale)
            for coefficient, value, mean, scale in zip(
                model.coefficients,
                vector,
                model.feature_means,
                model.feature_scales,
                strict=True,
            )
        ]
        contributions = dict(zip(INTERACTION_FEATURE_NAMES, terms, strict=True))
        for name, value in contributions.items():
            absolute_contributions[name].append(abs(value))
        predicted_log_ratio = model.intercept_log_ratio + sum(terms)
        observed_ratio = float(audited["median_early_over_late_ratio"])
        v1 = choose_mask_placement(features)
        v3 = cast(dict[str, Any], family["optimizer_v3"])
        reason_counts[str(v3["reason_code"])] += 1
        family_diagnostics.append(
            {
                "family_id": family_id,
                "stable_for_conclusion": bool(audited["stable_for_transfer_conclusion"]),
                "join_input_rows": features.join_input_rows,
                "identifier_width_bytes": features.identifier_width_bytes,
                "achieved_join_match_rate": features.join_match_rate,
                "support_vector": list(interaction_support_vector(features)),
                "within_training_support": model.is_within_training_support(features),
                "observed_paired_early_late_ratio": observed_ratio,
                "observed_log_early_late_ratio": math.log(observed_ratio),
                "predicted_log_early_late_ratio": predicted_log_ratio,
                "predicted_early_late_ratio": math.exp(predicted_log_ratio),
                "prediction_log_error": predicted_log_ratio - math.log(observed_ratio),
                "primary_model_direction": (
                    MaskPlacement.EARLY.value
                    if predicted_log_ratio < 0.0
                    else MaskPlacement.LATE.value
                ),
                "final_v3_selection": v3["selected_candidate_id"],
                "v3_reason_code": v3["reason_code"],
                "v1_selection": (
                    "early_mask_materialized"
                    if v1.placement == MaskPlacement.EARLY
                    else "late_mask"
                ),
                "v1_early_late_proxy_ratio": (v1.early_proxy_work_bytes / v1.late_proxy_work_bytes),
                "feature_contributions_to_predicted_log_ratio": contributions,
                "nearest_synthetic_training_families": _nearest_training_observations(
                    training, features
                ),
            }
        )
    stable = [item for item in family_diagnostics if item["stable_for_conclusion"]]
    primary_correct = sum(
        (
            item["primary_model_direction"] == MaskPlacement.EARLY.value
            and item["observed_paired_early_late_ratio"] < 0.97
        )
        or (
            item["primary_model_direction"] == MaskPlacement.LATE.value
            and item["observed_paired_early_late_ratio"] > 1.03
        )
        for item in stable
    )
    rows = int(family_diagnostics[0]["join_input_rows"])
    v1_product_threshold = v1_early_required_match_width_product(rows)
    max_observed_product = max(
        float(item["identifier_width_bytes"]) * float(item["achieved_join_match_rate"])
        for item in family_diagnostics
    )
    dominant = sorted(
        (
            {
                "feature": name,
                "mean_absolute_log_contribution": statistics.mean(values),
            }
            for name, values in absolute_contributions.items()
        ),
        key=lambda item: cast(float, item["mean_absolute_log_contribution"]),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "status": "ROOT_CAUSE_DIAGNOSED_V3_REMAINS_FROZEN",
        "source_run_id": directory.name,
        "family_count": len(family_diagnostics),
        "stable_family_count": len(stable),
        "final_v3_late_selection_count": sum(
            item["final_v3_selection"] == "late_mask" for item in family_diagnostics
        ),
        "primary_model_late_direction_count": sum(
            item["primary_model_direction"] == MaskPlacement.LATE.value
            for item in family_diagnostics
        ),
        "primary_model_correct_direction_on_stable_families": primary_correct,
        "decision_reason_counts": dict(sorted(reason_counts.items())),
        "all_core_features_within_support": all(
            item["within_training_support"] for item in family_diagnostics
        ),
        "v1_fallback_collapse": {
            "fixed_early_materialization_setup_bytes": (
                MaskOptimizerConfig().early_materialization_setup_bytes
            ),
            "required_match_rate_times_width_bytes": v1_product_threshold,
            "maximum_observed_match_rate_times_width_bytes": max_observed_product,
            "early_can_win_v1_proxy_on_observed_grid": (
                max_observed_product > v1_product_threshold
            ),
        },
        "dominant_primary_model_contributions": dominant,
        "category_assessment": {
            "feature_distortion": {
                "verdict": "PARTIALLY_CONFIRMED_SEMANTIC_INSUFFICIENCY_NOT_RANGE_ERROR",
                "evidence": (
                    "Rows, controlled sensitive width, and achieved match rate are exact and "
                    "inside support, but they omit total carried row width, storage/expression "
                    "path, output schema, sort shape, and pipeline-breaker behavior."
                ),
            },
            "cost_omissions": {
                "verdict": "CONFIRMED_FOR_PHYSICAL_PIPELINE_COMPONENTS",
                "evidence": (
                    "The model ranks a complete ratio from three logical statistics and has no "
                    "separate terms for hash-all versus hash-matched work, bytes through Join, "
                    "masked-boundary write/read, or downstream sort/project effects. Policy "
                    "filter and source-lineage costs are common or outside timing here and do "
                    "not explain the relative failure."
                ),
            },
            "decision_boundary_collapse": {
                "verdict": "CONFIRMED",
                "evidence": (
                    "The primary model points late in 11/12 families. Uncertain or ridge-"
                    "disagreement cases fall back to V1, whose fixed 256 MiB setup term makes "
                    "early mathematically unreachable on the observed grid."
                ),
            },
            "estimate_vs_measurement_deviation": {
                "verdict": "RATIO_CALIBRATION_ERROR_CONFIRMED_CORE_CARDINALITY_ERROR_REJECTED",
                "evidence": (
                    "Actual post-filter rows and achieved Join rates are passed to V3 and match "
                    "execution cardinalities; nevertheless predicted ratios have the wrong sign "
                    "for all six stable early families. One timing-unstable family was isolated."
                ),
            },
        },
        "family_diagnostics": family_diagnostics,
        "conclusion": (
            "The all-late behavior is caused by domain-shifted cost labels plus an all-late V1 "
            "fallback, not by out-of-support inputs or a collapsed DuckDB candidate space. "
            "Refitting the same polynomial or adjusting one threshold is not justified. A V4 "
            "must estimate legal candidates from decomposed physical work and real operator "
            "statistics, while keeping governance feasibility as a hard constraint."
        ),
    }
