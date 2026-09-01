"""Tests for fail-closed publication result bookkeeping."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trustaero.reproducibility.paper_results import verify_paper_results_registry


def _write_registry(root: Path, expected_sha256: str) -> Path:
    """Create the smallest valid registry around one frozen result."""

    record = root / "experiments/frozen/result.json"
    result = root / "results/run/evaluation.json"
    record.parent.mkdir(parents=True)
    result.parent.mkdir(parents=True)
    record.write_text('{"status":"PASS"}', encoding="utf-8")
    result.write_text('{"metric":1}', encoding="utf-8")
    registry = root / "experiments/frozen/registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "result",
                        "frozen_record": {
                            "path": "experiments/frozen/result.json",
                            "sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
                        },
                        "primary_artifacts": [
                            {
                                "path": "results/run/evaluation.json",
                                "sha256": expected_sha256,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return registry


def test_registry_verifies_bound_artifacts(tmp_path: Path) -> None:
    expected = hashlib.sha256(b'{"metric":1}').hexdigest()
    registry = _write_registry(tmp_path, expected)

    verification = verify_paper_results_registry(tmp_path, registry)

    assert verification.status == "PASS"
    assert verification.entry_count == 1
    assert verification.artifact_count == 2


def test_registry_rejects_changed_result(tmp_path: Path) -> None:
    expected = hashlib.sha256(b'{"metric":1}').hexdigest()
    registry = _write_registry(tmp_path, expected)
    (tmp_path / "results/run/evaluation.json").write_text('{"metric":2}', encoding="utf-8")

    verification = verify_paper_results_registry(tmp_path, registry)

    assert verification.status == "FAIL"
    assert verification.checks[-1].status == "MISMATCH"


def test_registry_rejects_path_traversal(tmp_path: Path) -> None:
    registry = _write_registry(tmp_path, "0" * 64)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["entries"][0]["primary_artifacts"][0]["path"] = "../outside.json"
    registry.write_text(json.dumps(payload), encoding="utf-8")

    verification = verify_paper_results_registry(tmp_path, registry)

    assert verification.status == "FAIL"
    assert verification.checks[-1].status == "INVALID"
