"""Tests for the non-cherry-picked official-query support audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trustaero.experiments.tpch_audit import (
    TPCH_IR_V1_BLOCKERS,
    TpchAuditError,
    _markdown_report,
    verify_tpch_artifact,
)


def test_tpch_audit_keeps_all_queries_and_only_claims_reviewed_queries() -> None:
    assert tuple(TPCH_IR_V1_BLOCKERS) == tuple(range(1, 23))
    assert [number for number, blockers in TPCH_IR_V1_BLOCKERS.items() if not blockers] == [1, 6]
    assert all(TPCH_IR_V1_BLOCKERS[number] for number in range(1, 23) if number not in {1, 6})


def test_tpch_report_makes_semantic_boundary_visible() -> None:
    payload = {
        "ir_v1_supported_count": 2,
        "ir_v1_supported_queries": ["Q01", "Q06"],
        "queries": [
            {
                "query_id": "Q06",
                "duckdb_status": "PASS",
                "ir_v1_status": "SUPPORTED",
                "blockers": [],
                "output_row_count": 1,
            },
            {
                "query_id": "Q07",
                "duckdb_status": "PASS",
                "ir_v1_status": "BLOCKED",
                "blockers": ["derived_projection"],
                "output_row_count": 4,
            },
        ],
    }

    report = _markdown_report(payload)

    assert "not a performance result" in report
    assert "Exact IR support: 2/22 (Q01, Q06)" in report
    assert "derived_projection" in report


def test_scale_qualified_artifact_verification(tmp_path: Path) -> None:
    database = tmp_path / "data/processed/tpch/sf10/tpch_sf10.duckdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"deterministic-test-database")
    manifest = tmp_path / "data/manifests/processed/tpch-sf10.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "scale_factor": 10,
                "database_path": "data/processed/tpch/sf10/tpch_sf10.duckdb",
                "byte_size": database.stat().st_size,
                "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    verified, payload = verify_tpch_artifact(tmp_path, scale_factor=10)

    assert verified == database
    assert payload["scale_factor"] == 10
    with pytest.raises(TpchAuditError, match="Unsupported reviewed"):
        verify_tpch_artifact(tmp_path, scale_factor=3)
