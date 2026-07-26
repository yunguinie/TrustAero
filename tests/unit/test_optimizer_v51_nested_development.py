from __future__ import annotations

import math
from typing import Any, cast

import pytest

from trustaero.experiments.optimizer_v5_hybrid_development import (
    V5DevelopmentGates,
    fit_v5_residual_surface,
)
from trustaero.experiments.optimizer_v51_nested_development import (
    deterministic_inner_folds,
    fit_v51_weighted_surface,
    select_severity_exponent,
    severity_weight,
)
from trustaero.experiments.pipeline_optimizer import PipelineMaskFamilyObservation
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_mechanism import (
    HASH_FEATURE_NAMES,
    JOIN_FEATURE_NAMES,
    MATERIALIZATION_FEATURE_NAMES,
    MechanismMaskCostModel,
    NonnegativeMechanismCost,
)
from trustaero.optimizer.mask_pipeline_v5 import PipelineV5HybridModel


def _component(name: str, names: tuple[str, ...]) -> NonnegativeMechanismCost:
    return NonnegativeMechanismCost(
        component_name=name,
        feature_names=names,
        intercept_ms=2.0,
        coefficients=tuple(1.0 for _ in names),
        ridge_lambda=0.1,
        training_group_count=5,
        source_run_ids=("micro",),
    )


def _prior() -> MechanismMaskCostModel:
    return MechanismMaskCostModel(
        hash_cost=_component("sha256", HASH_FEATURE_NAMES),
        materialization_cost=_component("materialization_roundtrip", MATERIALIZATION_FEATURE_NAMES),
        join_cost=_component("hash_join", JOIN_FEATURE_NAMES),
    )


def _observations() -> list[PipelineMaskFamilyObservation]:
    prior = _prior()
    output: list[PipelineMaskFamilyObservation] = []
    for index in range(15):
        features = MaskPlacementFeatures(
            join_input_rows=50_000 + index * 15_000,
            identifier_width_bytes=64 * (1 + index % 5),
            join_match_rate=0.08 + 0.05 * index,
        )
        # Alternate weak and strong separations so weighting has useful variation.
        separation = (-1.0 if index % 2 else 1.0) * (0.015 + 0.012 * index)
        late = prior.predict_candidate_ms(features, MaskPlacement.LATE)
        early = late * math.exp(separation)
        output.append(
            PipelineMaskFamilyObservation(
                family_id=f"family-{index:02d}",
                source_run_ids=("pipeline",),
                source_commit_hashes=("abc",),
                seed_count=3,
                features=features,
                median_early_latency_ms=early,
                median_late_latency_ms=late,
                observed_log_early_late_ratio=separation,
                tie_threshold_fraction=0.03,
            )
        )
    return output


def _lax_gates() -> V5DevelopmentGates:
    return V5DevelopmentGates(
        minimum_direct_coverage=0.0,
        minimum_within_tie_improvement_over_v1=-1.0,
        maximum_mean_regret_percent=1_000_000.0,
        maximum_p95_regret_percent=1_000_000.0,
        maximum_max_regret_percent=1_000_000.0,
    )


def test_severity_weight_keeps_unweighted_baseline_and_caps_extremes() -> None:
    weak, strong = _observations()[1], _observations()[-1]

    assert severity_weight(strong, 0.0, cap=16.0) == 1.0
    assert severity_weight(strong, 1.0, cap=16.0) > severity_weight(weak, 1.0, cap=16.0)
    assert severity_weight(strong, 100.0, cap=4.0) == 4.0
    with pytest.raises(ValueError, match="invalid"):
        severity_weight(strong, -1.0, cap=16.0)


def test_inner_folds_are_deterministic_balanced_and_family_indivisible() -> None:
    observations = _observations()
    first = deterministic_inner_folds(observations, fold_count=5, seed=27)
    second = deterministic_inner_folds(list(reversed(observations)), fold_count=5, seed=27)

    assert [[item.family_id for item in fold] for fold in first] == [
        [item.family_id for item in fold] for fold in second
    ]
    assert {len(fold) for fold in first} == {3}
    assert sorted(item.family_id for fold in first for item in fold) == sorted(
        item.family_id for item in observations
    )


def test_weighted_surface_retains_frozen_v5_serialization_contract() -> None:
    observations = _observations()
    surface = fit_v51_weighted_surface(
        observations,
        _prior(),
        severity_exponent=1.0,
        severity_weight_cap=16.0,
        ridge_lambda=0.1,
        uncertainty_multiplier=1.0,
    )
    model = PipelineV5HybridModel(_prior(), surface)

    assert surface.training_family_count == len(observations)
    assert surface.residual_log_ratio_rmse >= 0.0
    assert PipelineV5HybridModel.from_dict(model.to_dict()) == model


def test_zero_exponent_matches_the_frozen_unweighted_v5_objective() -> None:
    observations = _observations()
    frozen = fit_v5_residual_surface(
        observations,
        _prior(),
        ridge_lambda=0.1,
        uncertainty_multiplier=1.0,
    )
    direct = fit_v51_weighted_surface(
        observations,
        _prior(),
        severity_exponent=0.0,
        severity_weight_cap=16.0,
        ridge_lambda=0.1,
        uncertainty_multiplier=1.0,
    )

    assert direct.intercept_log_ms == pytest.approx(frozen.intercept_log_ms, abs=1e-8)
    assert direct.coefficients == pytest.approx(frozen.coefficients, abs=1e-8)


def test_nested_selection_compares_unweighted_and_weighted_objectives() -> None:
    exponent, candidates = select_severity_exponent(
        _observations(),
        _prior(),
        severity_exponents=(0.0, 1.0, 2.0),
        severity_weight_cap=16.0,
        fold_count=5,
        partition_seed=20260730,
        ridge_lambda=0.1,
        uncertainty_multiplier=1.0,
        gates=_lax_gates(),
    )

    assert exponent in {0.0, 1.0, 2.0}
    assert {float(cast(Any, item["severity_exponent"])) for item in candidates} == {
        0.0,
        1.0,
        2.0,
    }
    assert all(len(cast(list[float], item["selection_score"])) == 7 for item in candidates)
