"""L1 tests: strict typed contracts reject silent schema drift."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from trustaero.ir.models import (
    CandidatePlan,
    PhysicalOperatorPlacementSpec,
    PhysicalStrategySpec,
)


def test_candidate_round_trip_is_stable(accept_plan: dict[str, Any]) -> None:
    model = CandidatePlan.model_validate(accept_plan)
    assert CandidatePlan.model_validate(model.model_dump(mode="json")) == model


def test_unknown_field_is_forbidden(accept_plan: dict[str, Any]) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["debug_only"] = True
    with pytest.raises(ValidationError):
        CandidatePlan.model_validate(raw)


def test_negative_radius_is_rejected(rewrite_plan: dict[str, Any]) -> None:
    raw = copy.deepcopy(rewrite_plan)
    raw["operators"][1]["radius_km"] = -1
    with pytest.raises(ValidationError):
        CandidatePlan.model_validate(raw)


def test_combined_mask_strategy_can_only_materialize_the_moved_operator() -> None:
    """The combined mode is a tiny reviewed fragment, not arbitrary composition."""

    placement = PhysicalOperatorPlacementSpec(
        operator_id="mask-sensitive-id",
        after_operator_id="project-sensitive-id",
    )
    strategy = PhysicalStrategySpec(
        strategy_id="early-mask-boundary",
        execution_mode="governance_placed_materialized",
        materialize_after=("mask-sensitive-id",),
        placements=(placement,),
    )
    assert strategy.materialize_after == (placement.operator_id,)

    with pytest.raises(ValidationError, match="moved operator"):
        PhysicalStrategySpec(
            strategy_id="unsafe-composition",
            execution_mode="governance_placed_materialized",
            materialize_after=("some-other-operator",),
            placements=(placement,),
        )
