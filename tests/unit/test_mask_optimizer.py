"""Unit tests for the bounded, explainable Mask Optimizer V1."""

from __future__ import annotations

import pytest

from trustaero.optimizer.mask import (
    MaskPlacement,
    MaskPlacementFeatures,
    choose_mask_placement,
)


def test_large_wide_high_match_workload_selects_early_mask() -> None:
    decision = choose_mask_placement(
        MaskPlacementFeatures(
            join_input_rows=300_000,
            identifier_width_bytes=1024,
            join_match_rate=1.0,
        )
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.early_proxy_work_bytes < decision.late_proxy_work_bytes


@pytest.mark.parametrize(
    ("row_count", "width", "match_rate"),
    [(100_000, 1024, 1.0), (300_000, 1024, 0.1), (300_000, 18, 1.0)],
)
def test_setup_or_avoidable_hash_work_selects_late_mask(
    row_count: int,
    width: int,
    match_rate: float,
) -> None:
    decision = choose_mask_placement(
        MaskPlacementFeatures(
            join_input_rows=row_count,
            identifier_width_bytes=width,
            join_match_rate=match_rate,
        )
    )

    assert decision.placement is MaskPlacement.LATE


def test_zero_exposure_policy_forces_early_mask_before_cost_comparison() -> None:
    decision = choose_mask_placement(
        MaskPlacementFeatures(
            join_input_rows=100_000,
            identifier_width_bytes=18,
            join_match_rate=0.1,
            max_raw_exposure_rows=0,
        )
    )

    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_OPTIMIZER_LATE_INFEASIBLE"
    assert decision.estimated_early_raw_exposure_rows == 0


def test_selector_rejects_when_no_candidate_is_semantically_legal() -> None:
    with pytest.raises(ValueError, match="No legal Mask placement"):
        choose_mask_placement(
            MaskPlacementFeatures(
                join_input_rows=10,
                identifier_width_bytes=18,
                join_match_rate=1.0,
                early_mask_legal=False,
                late_mask_legal=False,
            )
        )


def test_feature_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="join_match_rate"):
        MaskPlacementFeatures(
            join_input_rows=10,
            identifier_width_bytes=18,
            join_match_rate=1.1,
        )
