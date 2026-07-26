"""Tests that bind EA-0 candidates to the new physical-work contract."""

from __future__ import annotations

from trustaero.experiments.execution_flow_audit import (
    ExecutionFlowUnit,
    execution_flow_variants,
)
from trustaero.experiments.execution_flow_features import (
    execution_flow_candidate_spec,
)
from trustaero.optimizer.execution_aware import derive_execution_aware_work


def _work(variant_id: str, *, match: float = 0.1, width: int = 512):
    variants = {item.variant_id: item for item in execution_flow_variants()}
    unit = ExecutionFlowUnit(1_000, width, match, 17)
    spec = execution_flow_candidate_spec(unit, variants[variant_id])
    return spec, derive_execution_aware_work(spec).as_dict()


def test_key_only_join_work_does_not_depend_on_pruned_sensitive_width() -> None:
    _narrow_spec, narrow = _work("join_key_only_aggregate", width=256)
    _wide_spec, wide = _work("join_key_only_aggregate", width=2_048)

    assert narrow["scan.payload_gib"] == wide["scan.payload_gib"]
    assert narrow["join.probe_payload_gib"] == wide["join.probe_payload_gib"]


def test_mask_rows_follow_candidate_placement_and_match_rate() -> None:
    early_spec, early = _work("prejoin_mask_materialized_output", match=0.1)
    late_spec, late = _work("postjoin_mask_fused_output", match=0.1)

    assert early["mask.materialized_input.rows_million"] == 0.001
    assert late["mask.fused_expression.rows_million"] == 0.0001
    assert early_spec.exposure is not None
    assert late_spec.exposure is not None
    assert early_spec.exposure.raw_rows_exposed_to_join == 0
    assert late_spec.exposure.raw_rows_exposed_to_join == 1_000


def test_raw_materialized_aggregate_has_distinct_pipeline_features() -> None:
    raw_spec, raw = _work("postjoin_raw_materialized_mask_aggregate", match=1.0)
    fused_spec, fused = _work("postjoin_mask_fused_aggregate", match=1.0)

    assert raw["materialization.raw.write_gib"] > 0.0
    assert raw["aggregate.materialized_input.masked_digest.rows_million"] == 0.001
    assert fused["aggregate.fused_expression.masked_digest.rows_million"] == 0.001
    assert "pipeline_breaker.raw_materialization.count" in raw
    assert "pipeline_breaker.raw_materialization.count" not in fused
    assert raw_spec.exposure is not None
    assert fused_spec.exposure is not None
    assert raw_spec.exposure.raw_rows_materialized == 1_000
    assert fused_spec.exposure.raw_rows_materialized == 0


def test_all_ea0_variants_produce_id_bound_valid_specs() -> None:
    unit = ExecutionFlowUnit(100, 64, 0.5, 3)

    specs = [execution_flow_candidate_spec(unit, item) for item in execution_flow_variants()]

    assert len(specs) == 11
    assert len({item.physical_plan_id for item in specs}) == 11
    assert all(item.exposure is not None for item in specs)
