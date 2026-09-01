"""L2 tests: graph errors must fail before policy evaluation."""

from __future__ import annotations

import copy
from typing import Any

from trustaero.ir.enums import ReasonCode
from trustaero.ir.models import CandidatePlan
from trustaero.validator.service import validate_graph


def codes(plan: dict[str, Any]) -> set[ReasonCode]:
    parsed = CandidatePlan.model_validate(plan)
    return {diagnostic.code for diagnostic in validate_graph(parsed)}


def test_unbound_reference(accept_plan: dict[str, Any]) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"][1]["inputs"] = ["missing"]
    assert ReasonCode.UNBOUND_REFERENCE in codes(raw)


def test_cycle(accept_plan: dict[str, Any]) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"][0]["inputs"] = ["op2"]
    assert ReasonCode.CYCLIC_PLAN in codes(raw)


def test_duplicate_id(accept_plan: dict[str, Any]) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["operators"][1]["operator_id"] = "op1"
    raw["output_operator"] = "op1"
    assert ReasonCode.DUPLICATE_OPERATOR_ID in codes(raw)


def test_operator_input_arity(accept_plan: dict[str, Any]) -> None:
    """A unary filter with no parent is valid JSON but not a valid plan graph."""

    raw = copy.deepcopy(accept_plan)
    raw["operators"][1]["inputs"] = []
    assert ReasonCode.INVALID_OPERATOR_ARGUMENT in codes(raw)


def test_deep_linear_plan_does_not_depend_on_python_recursion_limit(
    accept_plan: dict[str, Any],
) -> None:
    raw = copy.deepcopy(accept_plan)
    raw["plan_id"] = "pc-deep-graph"
    raw["operators"] = [raw["operators"][0]]
    previous = "op1"
    for index in range(1, 1500):
        current = f"op{index + 1}"
        raw["operators"].append(
            {
                "operator_type": "Project",
                "operator_id": current,
                "inputs": [previous],
                "fields": ["event_id", "magnitude"],
            }
        )
        previous = current
    raw["output_operator"] = previous
    assert codes(raw) == set()
