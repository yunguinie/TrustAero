"""L1 tests: strict typed contracts reject silent schema drift."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from trustaero.ir.models import CandidatePlan


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
