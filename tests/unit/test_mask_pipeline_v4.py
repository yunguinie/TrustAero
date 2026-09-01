"""Tests for the pre-model Optimizer V4 physical-work contract."""

from __future__ import annotations

import pytest

from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import (
    V4_WORK_FEATURE_NAMES,
    RealPipelineWorkloadStats,
    candidate_work_delta,
    derive_candidate_pipeline_work,
)


def _stats(**overrides: object) -> RealPipelineWorkloadStats:
    values: dict[str, object] = {
        "source_scan_rows": 1_000_000,
        "join_input_rows": 100_000,
        "join_output_rows_estimate": 70_000,
        "dimension_build_rows": 135,
        "sensitive_raw_width_bytes": 384.0,
        "source_scan_payload_width_bytes": 32.0,
        "join_fact_fixed_width_bytes": 16.0,
        "dimension_build_payload_width_bytes": 24.0,
        "dimension_output_payload_width_bytes": 16.0,
        "output_fixed_width_bytes": 32.0,
        "sort_key_width_bytes": 11.0,
        "statistic_provenance": "catalog_exact_controlled",
    }
    values.update(overrides)
    return RealPipelineWorkloadStats(**values)  # type: ignore[arg-type]


def test_v4_work_contract_distinguishes_early_and_late_physical_work() -> None:
    stats = _stats()

    early = derive_candidate_pipeline_work(stats, MaskPlacement.EARLY)
    late = derive_candidate_pipeline_work(stats, MaskPlacement.LATE)

    assert early.pre_join_hash_rows == 100_000
    assert early.post_join_hash_rows == 0
    assert early.boundary_rows == 100_000
    assert early.pipeline_breaker is True
    assert early.estimated_raw_join_exposure_rows == 0
    assert late.pre_join_hash_rows == 0
    assert late.post_join_hash_rows == 70_000
    assert late.boundary_rows == 0
    assert late.pipeline_breaker is False
    assert late.estimated_raw_join_exposure_rows == 100_000
    assert early.join_fact_payload_bytes < late.join_fact_payload_bytes
    assert len(early.feature_vector()) == len(V4_WORK_FEATURE_NAMES)
    assert candidate_work_delta(stats)[-1] == 1.0


def test_v4_governance_excludes_late_before_work_derivation() -> None:
    stats = _stats(max_raw_exposure_rows=0)

    assert stats.placement_is_legal(MaskPlacement.EARLY) is True
    assert stats.placement_is_legal(MaskPlacement.LATE) is False
    with pytest.raises(ValueError, match="not governance-feasible"):
        derive_candidate_pipeline_work(stats, MaskPlacement.LATE)


def test_v4_fragment_rejects_cardinality_expanding_join() -> None:
    with pytest.raises(ValueError, match="filtering many-to-one"):
        _stats(join_output_rows_estimate=100_001)


def test_v4_fragment_rejects_nonempty_zero_width_source() -> None:
    with pytest.raises(ValueError, match="positive scan payload"):
        _stats(source_scan_payload_width_bytes=0.0)
