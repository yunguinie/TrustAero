"""Minimal offline CLI; it delegates all logic to validator.service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.ir.models import PolicySet
from trustaero.validator.service import validate


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an untrusted TrustAero plan.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("plan", type=Path)
    validate_parser.add_argument("--policy", type=Path, required=True)
    validate_parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()

    policy = PolicySet.model_validate(_load(args.policy))
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load(args.catalog)))
    result = validate(_load(args.plan), policy, catalog)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
