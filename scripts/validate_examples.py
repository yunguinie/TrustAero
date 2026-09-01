"""Validate every example plan and print its deterministic outcome."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.ir.models import PolicySet
from trustaero.validator.service import validate

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    policy = PolicySet.model_validate(load(ROOT / "examples/policies/research_policy.json"))
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(load(ROOT / "examples/catalogs/minimal_catalog.json"))
    )
    for plan_path in sorted((ROOT / "examples/plans").glob("*.json")):
        result = validate(load(plan_path), policy, catalog)
        print(f"{plan_path.name}: {result.status.value}")


if __name__ == "__main__":
    main()
