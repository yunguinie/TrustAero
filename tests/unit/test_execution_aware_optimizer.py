"""Tests for the Execution-Aware physical-work and analytic ranking contract."""

from __future__ import annotations

from dataclasses import replace

import pytest

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.execution_aware import (
    ActiveColumn,
    AnalyticExecutionCostModel,
    AnalyticFeatureRate,
    ExecutionAwareCandidateSpec,
    FeatureSupportBound,
    derive_execution_aware_work,
    rank_execution_aware_candidates,
)


def _candidate(candidate_id: str = "fused") -> ExecutionAwareCandidateSpec:
    key = ActiveColumn("join_key", 8)
    marker = ActiveColumn("marker", 8)
    raw = ActiveColumn("sensitive_value", 512, "raw", sensitive=True)
    hashed = ActiveColumn("masked_value", 64, "hashed", sensitive=True)
    return ExecutionAwareCandidateSpec(
        candidate_id=candidate_id,
        physical_plan_id=f"plan-{candidate_id}",
        statistic_provenance="catalog_exact_controlled",
        scan_rows=1_000,
        scan_columns=(key, raw),
        join_build_rows=100,
        join_build_columns=(key, marker),
        join_probe_rows=1_000,
        join_probe_columns=(key,),
        join_output_rows=100,
        join_output_columns=(raw, marker),
        mask_rows=100,
        mask_input_columns=(raw,),
        mask_mode="fused_expression",
        result_rows=100,
        result_columns=(hashed, marker),
        exposure=CandidateExposure(candidate_id, 1_000, 0),
    )


def _model(*specs: ExecutionAwareCandidateSpec) -> AnalyticExecutionCostModel:
    feature_names = sorted(
        {name for spec in specs for name, _value in derive_execution_aware_work(spec).features}
    )
    return AnalyticExecutionCostModel(
        calibration_id="unit-test-calibration",
        rates=tuple(AnalyticFeatureRate(name, 1.0) for name in feature_names),
        support_bounds=tuple(FeatureSupportBound(name, 0.0, 1_000.0) for name in feature_names),
        stable_legal_preference=tuple(spec.candidate_id for spec in specs),
    )


def test_pruned_sensitive_width_does_not_leak_into_join_work() -> None:
    narrow = _candidate()
    wider_raw = ActiveColumn("sensitive_value", 2_048, "raw", sensitive=True)
    wide = replace(
        narrow,
        scan_columns=(narrow.scan_columns[0], wider_raw),
        join_output_columns=(wider_raw, narrow.join_output_columns[1]),
        mask_input_columns=(wider_raw,),
    )

    narrow_work = derive_execution_aware_work(narrow).as_dict()
    wide_work = derive_execution_aware_work(wide).as_dict()

    # The wide value affects scan, output, and Mask work, but DuckDB's pruned
    # Join probe payload still contains only the 8-byte key.
    assert narrow_work["join.probe_payload_gib"] == wide_work["join.probe_payload_gib"]
    assert wide_work["mask.fused_expression.input_gib"] == pytest.approx(
        4 * narrow_work["mask.fused_expression.input_gib"]
    )


def test_mask_and_materialization_work_are_candidate_specific() -> None:
    late = _candidate("late")
    raw = late.scan_columns[1]
    hashed = ActiveColumn("masked_value", 64, "hashed", sensitive=True)
    early = replace(
        late,
        candidate_id="early",
        physical_plan_id="plan-early",
        mask_rows=1_000,
        mask_mode="materialized_input",
        masked_materialization_rows=1_000,
        masked_materialization_columns=(hashed, late.scan_columns[0]),
        # This is the early-Mask route: the Join carries only the hashed value,
        # so its zero raw-Join exposure is consistent with the active schema.
        join_output_columns=(hashed, late.join_output_columns[1]),
        exposure=CandidateExposure("early", 0, 0, 1_000),
    )
    raw_boundary = replace(
        late,
        candidate_id="raw-boundary",
        physical_plan_id="plan-raw-boundary",
        raw_materialization_rows=100,
        raw_materialization_columns=(raw, late.join_output_columns[1]),
        aggregate_input_rows=100,
        aggregate_input_columns=(raw, late.join_output_columns[1]),
        aggregate_mode="materialized_input",
        aggregate_work_kind="raw_length",
        result_rows=1,
        result_columns=(ActiveColumn("count", 8),),
        pipeline_breaker_kinds=("raw_materialization",),
        exposure=CandidateExposure("raw-boundary", 1_000, 100),
    )

    early_work = derive_execution_aware_work(early).as_dict()
    late_work = derive_execution_aware_work(late).as_dict()
    raw_work = derive_execution_aware_work(raw_boundary).as_dict()

    assert early_work["mask.materialized_input.rows_million"] == pytest.approx(0.001)
    assert late_work["mask.fused_expression.rows_million"] == pytest.approx(0.0001)
    assert raw_work["materialization.raw.write_gib"] > 0.0
    assert raw_work["materialization.raw.read_gib"] > 0.0
    assert raw_work["aggregate.materialized_input.raw_length.rows_million"] > 0.0
    assert raw_work["pipeline_breaker.raw_materialization.count"] == 1.0


def test_illegal_candidate_is_removed_before_missing_cost_rate_is_read() -> None:
    hashed = ActiveColumn("masked_value", 64, "hashed", sensitive=True)
    legal = replace(
        _candidate("legal"),
        join_output_columns=(hashed, ActiveColumn("marker", 8)),
        exposure=CandidateExposure("legal", 0, 0),
    )
    illegal = replace(
        _candidate("illegal"),
        pipeline_breaker_kinds=("uncalibrated_illegal_breaker",),
        exposure=CandidateExposure("illegal", 1_000, 100),
    )
    model = _model(legal)
    policy = GovernanceFeasibilityPolicy("strict", 0, 0)

    result = rank_execution_aware_candidates((legal, illegal), policy, model)

    assert result.selected_candidate_id == "legal"
    assert result.reason_code == "EXECUTION_AWARE_ONLY_LEGAL_CANDIDATE"
    assert result.rejected_candidate_ids == ("illegal",)
    assert result.estimates == ()


def test_empty_legal_set_fails_closed_without_cost_ranking() -> None:
    raw = _candidate("raw-only")
    result = rank_execution_aware_candidates(
        (raw,),
        GovernanceFeasibilityPolicy("deny-raw", 0, 0),
        _model(raw),
    )

    assert result.status == "REJECT"
    assert result.selected_candidate_id is None
    assert result.reason_code == "EXECUTION_AWARE_NO_LEGAL_CANDIDATE"
    assert result.estimates == ()


def test_practical_tie_and_out_of_support_use_stable_legal_fallback() -> None:
    preferred = _candidate("preferred")
    other = replace(
        preferred,
        candidate_id="other",
        physical_plan_id="plan-other",
        exposure=CandidateExposure("other", 1_000, 0),
    )
    model = _model(preferred, other)
    policy = GovernanceFeasibilityPolicy("permissive", None, None)

    tied = rank_execution_aware_candidates((preferred, other), policy, model)
    assert tied.selected_candidate_id == "preferred"
    assert tied.reason_code == "EXECUTION_AWARE_PRACTICAL_TIE_FALLBACK"
    assert tied.practically_tied_candidate_ids == ("other", "preferred")

    tiny_support = replace(
        model,
        support_bounds=tuple(
            FeatureSupportBound(item.feature_name, 0.0, 0.0) for item in model.rates
        ),
    )
    fallback = rank_execution_aware_candidates((preferred, other), policy, tiny_support)
    assert fallback.selected_candidate_id == "preferred"
    assert fallback.reason_code == "EXECUTION_AWARE_OUT_OF_SUPPORT_FALLBACK"


def test_positive_feature_without_calibration_fails_closed() -> None:
    left = _candidate("left")
    right = replace(
        left,
        candidate_id="right",
        physical_plan_id="plan-right",
        pipeline_breaker_kinds=("new_breaker",),
        exposure=CandidateExposure("right", 1_000, 0),
    )

    with pytest.raises(ValueError, match="No calibrated analytic rate"):
        rank_execution_aware_candidates(
            (left, right),
            GovernanceFeasibilityPolicy("permissive", None, None),
            _model(left),
        )


def test_active_sensitive_columns_cannot_understate_governance_exposure() -> None:
    with pytest.raises(ValueError, match="Raw sensitive Join work"):
        replace(
            _candidate("dishonest"),
            exposure=CandidateExposure("dishonest", 0, 0),
        )


def test_lineage_work_is_part_of_every_candidate_cost_vector() -> None:
    candidate = replace(
        _candidate("lineage"),
        lineage_rows=100,
        lineage_edges=250,
        lineage_payload_width_bytes=48,
    )

    work = derive_execution_aware_work(candidate).as_dict()

    assert work["lineage.rows_million"] == pytest.approx(0.0001)
    assert work["lineage.edges_million"] == pytest.approx(0.00025)
    assert work["lineage.payload_gib"] == pytest.approx(100 * 48 / 1024**3)
