"""Freeze content-addressed SF10 Q1/Q6 timing configs after semantic gates.

This module never runs a timed query and never chooses a winning candidate.
It only binds a verified SF10 database, two result-equivalence smokes, and the
already reviewed IR-support audit to the predeclared paired timing protocol.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file
from trustaero.experiments.tpch_audit import TpchAuditError, verify_tpch_artifact


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TpchAuditError(f"Cannot read SF10 protocol input {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TpchAuditError(f"SF10 protocol input is not an object: {path}")
    return payload


def _require_semantic_smoke(path: Path, *, query_id: str, artifact_sha256: str) -> None:
    payload = _read_object(path)
    required = {
        "status": "PASS",
        "query_id": query_id,
        "scale_factor": 10,
        "artifact_sha256": artifact_sha256,
        "candidate_count": 3,
        "distinct_duckdb_plan_count": 3,
        "all_official_result_equivalent": True,
        "paper_performance_evidence": False,
    }
    mismatches = {
        key: (payload.get(key), expected)
        for key, expected in required.items()
        if payload.get(key) != expected
    }
    if mismatches:
        raise TpchAuditError(f"SF10 {query_id} semantic smoke is not admissible: {mismatches}")


def _require_support_audit(path: Path) -> None:
    payload = _read_object(path)
    if (
        payload.get("status") != "PASS"
        or payload.get("ir_v1_supported_queries") != ["Q01", "Q06"]
        or payload.get("official_query_count") != 22
    ):
        raise TpchAuditError("TPC-H support audit no longer binds exactly Q01/Q06 of 22")


def _write_deterministic_config(path: Path, payload: dict[str, Any]) -> str:
    """Write once, or verify an identical existing freeze without overwriting it."""

    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise TpchAuditError(f"Refusing to overwrite a different frozen config: {path}")
        return "verified_existing"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)
    return "created"


def freeze_tpch_sf10_protocol(project_root: Path) -> dict[str, Any]:
    """Create deterministic SF10 configs after all untimed prerequisites pass."""

    root = project_root.resolve()
    _, artifact = verify_tpch_artifact(root, scale_factor=10)
    artifact_sha256 = str(artifact["sha256"])
    q1_smoke = root / "results/tpch_sf10_q1_semantic_smoke/result.json"
    q6_smoke = root / "results/tpch_sf10_q6_semantic_smoke/result.json"
    support_audit = root / "results/tpch_sf1_support_audit_q01_q06_v3/audit.json"
    _require_semantic_smoke(q1_smoke, query_id="Q01", artifact_sha256=artifact_sha256)
    _require_semantic_smoke(q6_smoke, query_id="Q06", artifact_sha256=artifact_sha256)
    _require_support_audit(support_audit)

    shared = {
        "scale_factor": 10,
        "warmup_blocks": 6,
        "measured_blocks": 30,
        "duckdb_threads": 4,
        "duckdb_memory_limit_mb": 4096,
        "absolute_half_drift_limit": 0.5,
        "paired_ratio_half_drift_limit": 0.2,
        "paired_ratio_outlier_fraction_limit": 0.1,
        "tie_threshold_fraction": 0.03,
        "artifact_sha256": artifact_sha256,
        "support_audit_sha256": sha256_file(support_audit),
        "support_audit_path": support_audit.relative_to(root).as_posix(),
        "require_clean_git": True,
        "paper_performance_evidence": True,
        "heldout_optimizer_evidence": False,
        "timed_repeats_per_position": 5,
        "duckdb_timezone": "UTC",
        "bootstrap_repetitions": 20000,
        "carryover_tolerance_fraction": 0.1,
        "confidence_level": 0.95,
        "minimum_claim_blocks": 10,
    }
    configs = {
        root / "experiments/configs/tpch_sf10_q1_paired_ci_v2.json": {
            **shared,
            "results_dir": "results/tpch_sf10_q1_formal_v2",
            "order_seed": 20260725,
            "scientific_label": "tpch_sf10_q1_pollution_safe_paired_ci_v2",
            "timing_protocol": "exact_decimal_utc_pollution_safe_paired_ci_v2",
            "bootstrap_seed": 20260725,
            "carryover_candidate_ids": ["materialize-after-q01-filter"],
            "minimum_carryover_pairs": 5,
            "semantic_smoke_sha256": sha256_file(q1_smoke),
            "semantic_smoke_path": q1_smoke.relative_to(root).as_posix(),
        },
        root / "experiments/configs/tpch_sf10_q6_paired_ci_v2.json": {
            **shared,
            # With two predeclared carryover candidates, only one of the six
            # orders is safe for each baseline claim. Ten complete permutation
            # cycles preserve ten pollution-safe paired blocks per claim.
            "measured_blocks": 60,
            "results_dir": "results/tpch_sf10_q6_formal_v2",
            "order_seed": 20260726,
            "scientific_label": "tpch_sf10_q6_pollution_safe_paired_ci_v2",
            "timing_protocol": "exact_decimal_utc_pollution_safe_paired_ci_v4",
            "bootstrap_seed": 20260726,
            "carryover_candidate_ids": [
                "materialize-after-q06-time",
                "materialize-after-q06-predicate",
            ],
            "minimum_carryover_pairs": 10,
            "semantic_smoke_sha256": sha256_file(q6_smoke),
            "semantic_smoke_path": q6_smoke.relative_to(root).as_posix(),
        },
    }
    actions = {
        path.relative_to(root).as_posix(): _write_deterministic_config(path, payload)
        for path, payload in configs.items()
    }
    return {
        "status": "PASS",
        "artifact_sha256": artifact_sha256,
        "actions": actions,
        "scientific_boundary": (
            "Untimed semantic prerequisites are frozen; no performance result or "
            "optimizer winner was inspected while creating these configs."
        ),
    }
