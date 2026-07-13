"""Shared deterministic fixtures for the first TrustAero validation matrix."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.ir.models import PolicySet

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


@pytest.fixture
def catalog() -> InMemoryCatalog:
    return InMemoryCatalog(
        CatalogDocument.model_validate(load_json("examples/catalogs/minimal_catalog.json"))
    )


@pytest.fixture
def policy_set() -> PolicySet:
    return PolicySet.model_validate(load_json("examples/policies/research_policy.json"))


@pytest.fixture
def accept_plan() -> dict[str, Any]:
    return copy.deepcopy(load_json("examples/plans/accept_earthquakes.json"))


@pytest.fixture
def rewrite_plan() -> dict[str, Any]:
    return copy.deepcopy(load_json("examples/plans/rewrite_precision.json"))


@pytest.fixture
def clarify_plan() -> dict[str, Any]:
    return copy.deepcopy(load_json("examples/plans/clarify_purpose.json"))


@pytest.fixture
def reject_plan() -> dict[str, Any]:
    return copy.deepcopy(load_json("examples/plans/reject_public_facilities.json"))
