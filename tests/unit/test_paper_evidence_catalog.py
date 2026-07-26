"""Tests for publication-facing evidence catalog generation."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from trustaero.reproducibility.paper_catalog import (
    build_paper_evidence_catalog,
    write_paper_evidence_catalog,
)


def _create_registry(root: Path, *, result_bytes: bytes = b'{"metric":1}') -> Path:
    """Create one minimal registry entry and its two bound artifacts."""

    frozen = root / "experiments/frozen/result.json"
    result = root / "results/run/evaluation.json"
    frozen.parent.mkdir(parents=True)
    result.parent.mkdir(parents=True)
    frozen.write_bytes(b'{"status":"PASS"}')
    result.write_bytes(result_bytes)
    registry = root / "experiments/frozen/registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "registry_id": "test-registry",
                "updated_at": "2026-07-26",
                "entries": [
                    {
                        "entry_id": "semantic-result",
                        "evidence_role": "semantic_correctness",
                        "outcome": "positive",
                        "frozen_record": {
                            "path": "experiments/frozen/result.json",
                            "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
                        },
                        "primary_artifacts": [
                            {
                                "path": "results/run/evaluation.json",
                                "sha256": hashlib.sha256(result_bytes).hexdigest(),
                            }
                        ],
                        "authorized_claim": "The frozen case passed.",
                        "boundary": "Only this case is covered.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return registry


def test_catalog_contains_only_verified_registry_entries(tmp_path: Path) -> None:
    registry = _create_registry(tmp_path)

    catalog = build_paper_evidence_catalog(tmp_path, registry)

    assert catalog.entry_count == 1
    assert catalog.verified_artifact_count == 2
    assert catalog.outcome_counts == {"positive": 1}
    assert catalog.rows[0].authorized_claim == "The frozen case passed."


def test_catalog_fails_closed_after_artifact_mutation(tmp_path: Path) -> None:
    registry = _create_registry(tmp_path)
    (tmp_path / "results/run/evaluation.json").write_text('{"metric":2}', encoding="utf-8")

    with pytest.raises(ValueError, match="integrity verification"):
        build_paper_evidence_catalog(tmp_path, registry)


def test_catalog_writes_json_csv_and_markdown(tmp_path: Path) -> None:
    registry = _create_registry(tmp_path)
    catalog = build_paper_evidence_catalog(tmp_path, registry)

    json_path, csv_path, markdown_path = write_paper_evidence_catalog(catalog, tmp_path / "catalog")

    assert json.loads(json_path.read_text(encoding="utf-8"))["entry_count"] == 1
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["entry_id"] == "semantic-result"
    assert "The frozen case passed." in markdown_path.read_text(encoding="utf-8")
