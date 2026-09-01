"""Tests for V4.1 group-sign consensus decisions."""

from __future__ import annotations

from dataclasses import replace

from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import RealPipelineWorkloadStats
from trustaero.optimizer.mask_pipeline_v4_model import PipelineV4CostModel
from trustaero.optimizer.mask_pipeline_v41 import (
    PipelineV41SignEnsemble,
    choose_mask_placement_v41,
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


def _surface(intercept: float) -> PipelineV4CostModel:
    return PipelineV4CostModel(
        intercept_log_ratio=intercept,
        coefficients=(0.0,) * 4,
        feature_means=(0.0,) * 4,
        feature_scales=(1.0,) * 4,
        uncertainty_threshold=0.0,
        ridge_lambda=1.0,
        training_family_count=10,
        training_scenario_groups=("g",),
        support_join_input_rows=(50, 200),
        support_sensitive_width_bytes=(192.0, 1536.0),
        support_match_rate=(0.2, 1.0),
    )


def test_v41_unanimous_sign_makes_direct_decision() -> None:
    model = PipelineV41SignEnsemble(_surface(-0.2), (_surface(-0.1), _surface(-0.3)))
    decision = choose_mask_placement_v41(_stats(), model)
    assert decision.direct_placement == MaskPlacement.EARLY
    assert decision.direct_model_decision is True


def test_v41_sign_disagreement_is_uncertain() -> None:
    model = PipelineV41SignEnsemble(_surface(-0.2), (_surface(0.1), _surface(-0.3)))
    decision = choose_mask_placement_v41(_stats(), model)
    assert decision.direct_placement is None
    assert decision.reason_code == "MASK_V41_GROUP_SIGN_DISAGREEMENT"
    assert decision.conservative_fallback_placement == MaskPlacement.EARLY


def test_v41_governance_forces_early_before_consensus() -> None:
    model = PipelineV41SignEnsemble(_surface(0.2), (_surface(0.1), _surface(0.3)))
    decision = choose_mask_placement_v41(replace(_stats(), max_raw_exposure_rows=0), model)
    assert decision.direct_placement == MaskPlacement.EARLY
    assert decision.reason_code == "MASK_V41_LATE_INFEASIBLE"
