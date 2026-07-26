"""Contract tests for the untimed SF10 protocol freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trustaero.experiments.tpch_audit import TpchAuditError
from trustaero.experiments.tpch_q1_formal import load_tpch_q1_formal_config
from trustaero.experiments.tpch_q6_formal import load_tpch_q6_formal_config
from trustaero.experiments.tpch_sf10_protocol import freeze_tpch_sf10_protocol


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture_root(tmp_path: Path) -> Path:
    database = tmp_path / "data/processed/tpch/sf10/tpch_sf10.duckdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sf10-test-database")
    artifact_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _write_json(
        tmp_path / "data/manifests/processed/tpch-sf10.json",
        {
            "scale_factor": 10,
            "database_path": "data/processed/tpch/sf10/tpch_sf10.duckdb",
            "byte_size": database.stat().st_size,
            "sha256": artifact_sha,
        },
    )
    for query_id in ("Q01", "Q06"):
        directory_query = "q1" if query_id == "Q01" else "q6"
        _write_json(
            tmp_path / f"results/tpch_sf10_{directory_query}_semantic_smoke/result.json",
            {
                "status": "PASS",
                "query_id": query_id,
                "scale_factor": 10,
                "artifact_sha256": artifact_sha,
                "candidate_count": 3,
                "distinct_duckdb_plan_count": 3,
                "all_official_result_equivalent": True,
                "paper_performance_evidence": False,
            },
        )
    _write_json(
        tmp_path / "results/tpch_sf1_support_audit_q01_q06_v3/audit.json",
        {
            "status": "PASS",
            "ir_v1_supported_queries": ["Q01", "Q06"],
            "official_query_count": 22,
        },
    )
    return tmp_path


def test_sf10_freeze_creates_loadable_content_addressed_configs(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)

    first = freeze_tpch_sf10_protocol(root)
    second = freeze_tpch_sf10_protocol(root)
    q1 = load_tpch_q1_formal_config(root / "experiments/configs/tpch_sf10_q1_paired_ci_v2.json")
    q6 = load_tpch_q6_formal_config(root / "experiments/configs/tpch_sf10_q6_paired_ci_v2.json")

    assert first["status"] == "PASS"
    assert set(first["actions"].values()) == {"created"}
    assert set(second["actions"].values()) == {"verified_existing"}
    assert q1.scale_factor == q6.scale_factor == 10
    assert q1.measured_blocks == 30
    assert q6.measured_blocks == 60
    assert q1.timed_repeats_per_position == q6.timed_repeats_per_position == 5
    assert q1.confidence_level == q6.confidence_level == 0.95


def test_sf10_freeze_rejects_result_inequivalence(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    smoke = root / "results/tpch_sf10_q6_semantic_smoke/result.json"
    payload = json.loads(smoke.read_text(encoding="utf-8"))
    payload["all_official_result_equivalent"] = False
    _write_json(smoke, payload)

    with pytest.raises(TpchAuditError, match="not admissible"):
        freeze_tpch_sf10_protocol(root)
