from __future__ import annotations

import math

from trustaero.experiments.optimizer_v5_hybrid_development import (
    V5DevelopmentGates,
    evaluate_v5_fold,
    fit_v5_residual_surface,
    summarize_v5_predictions,
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
    for index in range(10):
        features = MaskPlacementFeatures(
            join_input_rows=50_000 + index * 20_000,
            identifier_width_bytes=64 * (1 + index % 4),
            join_match_rate=0.1 + 0.08 * index,
        )
        # The deterministic residual gives the fitter a real pipeline signal
        # while preserving positive absolute candidate latencies.
        early = prior.predict_candidate_ms(features, MaskPlacement.EARLY) * math.exp(
            0.04 * index + 0.1
        )
        late = prior.predict_candidate_ms(features, MaskPlacement.LATE) * math.exp(0.02 * index)
        output.append(
            PipelineMaskFamilyObservation(
                family_id=f"family-{index}",
                source_run_ids=("pipeline",),
                source_commit_hashes=("abc",),
                seed_count=3,
                features=features,
                median_early_latency_ms=early,
                median_late_latency_ms=late,
                observed_log_early_late_ratio=math.log(early / late),
                tie_threshold_fraction=0.03,
            )
        )
    return output


def test_fit_uses_mechanism_prior_and_serializes() -> None:
    observations = _observations()
    surface = fit_v5_residual_surface(
        observations,
        _prior(),
        ridge_lambda=0.1,
        uncertainty_multiplier=1.0,
    )
    model = PipelineV5HybridModel(_prior(), surface)
    assert surface.training_family_count == 10
    assert surface.residual_log_ratio_rmse >= 0.0
    assert PipelineV5HybridModel.from_dict(model.to_dict()) == model


def test_family_fold_and_summary_do_not_split_seeds() -> None:
    observations = _observations()
    rows = evaluate_v5_fold(
        observations[0],
        observations[1:],
        _prior(),
        ridge_lambda=0.1,
        uncertainty_multiplier=1.0,
    )
    assert {row["scheme"] for row in rows} == {"v1", "v5_direct", "v5_guarded"}
    assert {row["family_id"] for row in rows} == {"family-0"}
    assert {row["seed_count"] for row in rows} == {3}

    gates = V5DevelopmentGates(
        minimum_direct_coverage=0.0,
        minimum_within_tie_improvement_over_v1=-1.0,
        maximum_mean_regret_percent=1_000_000.0,
        maximum_p95_regret_percent=1_000_000.0,
        maximum_max_regret_percent=1_000_000.0,
    )
    summary = summarize_v5_predictions(rows, gates=gates)
    assert summary["status"] == "PASS_V5_HYBRID_DEVELOPMENT_GATE"
