"""Load Phase 0 experiment inputs without adding non-standard dependencies."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.experiments.models import ExperimentCase
from trustaero.ir.models import PolicySet


def load_json(path: Path) -> dict[str, Any]:
    """Read a JSON file as a plain dictionary."""

    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return loaded


def load_policy(path: Path) -> PolicySet:
    """Load a policy set through the public typed model."""

    return PolicySet.model_validate(load_json(path))


def load_catalog(path: Path) -> InMemoryCatalog:
    """Load a catalog document into the in-memory catalog implementation."""

    return InMemoryCatalog(CatalogDocument.model_validate(load_json(path)))


def load_cases(path: Path) -> tuple[ExperimentCase, ...]:
    """Load deterministic Phase 0 cases from CSV.

    CSV is used instead of YAML to keep the research artifact dependency-light.
    Reason codes are pipe-separated so a case may expect multiple diagnostics.
    """

    rows: list[ExperimentCase] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            expected_codes = tuple(
                code.strip()
                for code in row.get("expected_reason_codes", "").split("|")
                if code.strip()
            )
            rows.append(
                ExperimentCase(
                    case_id=row["case_id"],
                    case_category=row["case_category"],
                    plan_path=row["plan_path"],
                    policy_path=row["policy_path"],
                    catalog_path=row["catalog_path"],
                    expected_status=row["expected_status"],
                    expected_reason_codes=expected_codes,
                )
            )
    return tuple(sorted(rows, key=lambda item: item.case_id))
