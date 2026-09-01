"""Tests for bounded Pipeline-aware Optimizer V4 decisions."""

from __future__ import annotations

from dataclasses import replace

from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import RealPipelineWorkloadStats
from trustaero.optimizer.mask_pipeline_v4_model import (
    PIPELINE_V4_MODEL_FEATURE_NAMES,
    PipelineV4CostModel,
    choose_mask_placement_v4,
    pipeline_v4_model_feature_vector,
)


def _stats() -> RealPipelineWorkloadStats:
    return RealPipelineWorkloadStats(
        source_scan_rows=1000,
        join_input_rows=100,
        join_output_rows_estimate=70,
        dimension_build_rows=2,
        sensitive_raw_width_bytes=384.0,
        source_scan_payload_width_bytes=31.0,
        join_fact_fixed_width_bytes=16.0,
        dimension_build_payload_width_bytes=28.0,
        dimension_output_payload_width_bytes=20.0,
        output_fixed_width_bytes=8.0,
        sort_key_width_bytes=11.0,
        statistic_provenance="catalog_exact_controlled",
    )


def _model(intercept: float, uncertainty: float = 0.05) -> PipelineV4CostModel:
    return PipelineV4CostModel(
        intercept_log_ratio=intercept,
        coefficients=(0.0,) * len(PIPELINE_V4_MODEL_FEATURE_NAMES),
        feature_means=(0.0,) * len(PIPELINE_V4_MODEL_FEATURE_NAMES),
        feature_scales=(1.0,) * len(PIPELINE_V4_MODEL_FEATURE_NAMES),
        uncertainty_threshold=uncertainty,
        ridge_lambda=1.0,
        training_family_count=10,
        training_scenario_groups=("g1",),
        support_join_input_rows=(50, 200),
        support_sensitive_width_bytes=(192.0, 1536.0),
        support_match_rate=(0.2, 1.0),
    )


def test_v4_feature_vector_is_the_frozen_four_work_differences() -> None:
    assert len(pipeline_v4_model_feature_vector(_stats())) == 4


def test_v4_governance_precedes_cost_ranking() -> None:
    decision = choose_mask_placement_v4(replace(_stats(), max_raw_exposure_rows=0), _model(1.0))
    assert decision.placement == MaskPlacement.EARLY
    assert decision.reason_code == "MASK_V4_LATE_INFEASIBLE"


def test_v4_uncertainty_uses_conservative_early_fallback() -> None:
    decision = choose_mask_placement_v4(_stats(), _model(0.01))
    assert decision.placement == MaskPlacement.EARLY
    assert decision.used_conservative_fallback is True
    assert decision.direct_model_decision is False


def test_v4_confident_prediction_selects_both_directions() -> None:
    assert choose_mask_placement_v4(_stats(), _model(-0.2)).placement == MaskPlacement.EARLY
    assert choose_mask_placement_v4(_stats(), _model(0.2)).placement == MaskPlacement.LATE
