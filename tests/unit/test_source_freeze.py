"""Tests for publication source-freeze integrity helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trustaero.reproducibility.source_freeze import verify_frozen_records


def _write_record(root: Path, relative_path: str, digest: str) -> None:
    frozen = root / "experiments/frozen/fixture.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_text(
        json.dumps(
            {"schema_version": 1, "immutable_files": [{"path": relative_path, "sha256": digest}]}
        ),
        encoding="utf-8",
    )


def test_frozen_record_accepts_matching_sha256(tmp_path: Path) -> None:
    artifact = tmp_path / "results/run/summary.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"PASS"}\n', encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    _write_record(tmp_path, "results/run/summary.json", digest)

    record_count, checks = verify_frozen_records(tmp_path)

    assert record_count == 1
    assert len(checks) == 1
    assert checks[0].status == "PASS"


def test_frozen_record_detects_modified_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "results/run/summary.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("changed", encoding="utf-8")
    _write_record(tmp_path, "results/run/summary.json", "0" * 64)

    _, checks = verify_frozen_records(tmp_path)

    assert checks[0].status == "MISMATCH"


def test_frozen_record_rejects_path_traversal(tmp_path: Path) -> None:
    _write_record(tmp_path, "../outside.json", "0" * 64)

    _, checks = verify_frozen_records(tmp_path)

    assert checks[0].status == "INVALID_PATH"
