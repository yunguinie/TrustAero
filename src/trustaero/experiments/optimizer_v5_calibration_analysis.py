"""Pollution-safe paired inference for Optimizer V5 development calibration."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.paired_claims import (
    assess_carryover,
    authorize_paired_claims,
)
from trustaero.experiments.real_data_candidate_analysis import (
    analyze_real_data_candidate_pilot,
)
from trustaero.experiments.real_data_candidate_pilot import (
    load_candidate_pilot_config,
)
from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class V5CalibrationInferenceConfig:
    protocol_name: str
    measurement_config_path: str
    measurement_config_sha256: str
    source_negative_record_path: str
    source_negative_record_sha256: str
    confidence_level: float
    bootstrap_repetitions: int
    bootstrap_seed: int
    carryover_tolerance_fraction: float
    minimum_carryover_pairs: int
    minimum_claim_blocks: int
    tie_threshold_fraction: float
    minimum_model_eligible_units: int
    carryover_candidates_by_workload: dict[str, tuple[str, ...]]
    scientific_boundary: str


def load_v5_calibration_inference_config(
    path: Path | str,
) -> V5CalibrationInferenceConfig:
    payload = _load_json(Path(path))
    mappings = cast(dict[str, list[str]], payload["carryover_candidates_by_workload"])
    return V5CalibrationInferenceConfig(
        protocol_name=str(payload["protocol_name"]),
        measurement_config_path=str(payload["measurement_config_path"]),
        measurement_config_sha256=str(payload["measurement_config_sha256"]),
        source_negative_record_path=str(payload["source_negative_record_path"]),
        source_negative_record_sha256=str(payload["source_negative_record_sha256"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_repetitions=int(payload["bootstrap_repetitions"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        carryover_tolerance_fraction=float(payload["carryover_tolerance_fraction"]),
        minimum_carryover_pairs=int(payload["minimum_carryover_pairs"]),
        minimum_claim_blocks=int(payload["minimum_claim_blocks"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        minimum_model_eligible_units=int(payload["minimum_model_eligible_units"]),
        carryover_candidates_by_workload={
            workload: tuple(items) for workload, items in mappings.items()
        },
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _authorized_oracle_set(
    claims: list[dict[str, Any]],
    *,
    baseline_id: str = "fused",
) -> list[str] | None:
    """Translate conclusive baseline comparisons into a conservative label set."""

    if any(not bool(item["claim_authorized"]) for item in claims):
        return None
    faster = [
        str(item["candidate_id"]) for item in claims if item["conclusion"] == "MATERIALLY_FASTER"
    ]
    if len(faster) > 1:
        # Baseline comparisons cannot rank two candidates that both beat fused.
        return None
    if faster:
        return faster
    allowed = [baseline_id]
    allowed.extend(
        str(item["candidate_id"])
        for item in claims
        if item["conclusion"] == "PRACTICALLY_EQUIVALENT"
    )
    return sorted(allowed)


def _inference_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    by_unit: dict[str, list[dict[str, Any]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_unit.setdefault(row["unit_id"], []).append(
                {
                    "block_index": int(row["repeat_index"]),
                    "candidate_id": row["strategy_id"],
                    "permutation_id": row["permutation_id"],
                    "client_materialization_latency_ms": float(
                        row["client_materialization_latency_ms"]
                    ),
                }
            )
    return by_unit


def analyze_optimizer_v5_calibration(
    run_dir: Path,
    config: V5CalibrationInferenceConfig,
    *,
    project_root: Path,
) -> dict[str, object]:
    """Apply frozen carryover and CI rules without altering raw measurements."""

    root = project_root.resolve()
    measurement_config = root / config.measurement_config_path
    negative_record = root / config.source_negative_record_path
    if sha256_file(measurement_config) != config.measurement_config_sha256:
        raise ValueError("V5 measurement config binding changed")
    if sha256_file(negative_record) != config.source_negative_record_sha256:
        raise ValueError("V5 negative-result binding changed")
    # The runner persists the complete dataclass, including optional fields
    # whose source JSON may omit their ``null`` defaults.  Normalize through
    # the same public loader before comparing so omitted and explicit defaults
    # are equivalent, while every effective experimental parameter remains
    # fail-closed.
    expected_measurement_config = load_candidate_pilot_config(measurement_config)
    observed_measurement_config = load_candidate_pilot_config(run_dir / "config.json")
    if expected_measurement_config != observed_measurement_config:
        raise ValueError("Run does not use the frozen V5 V2 measurement config")

    legacy = analyze_real_data_candidate_pilot(
        run_dir,
        tie_threshold_fraction=config.tie_threshold_fraction,
    )
    _atomic_json(run_dir / "legacy_stability_analysis.json", legacy)
    summary = _load_json(run_dir / "summary.json")
    rows_by_unit = _inference_rows(run_dir / "measurements.csv")
    unit_results: list[dict[str, object]] = []
    for unit in cast(list[dict[str, Any]], summary["units"]):
        unit_id = str(unit["unit_id"])
        workload = str(unit["workload"])
        rows = rows_by_unit[unit_id]
        candidate_ids = tuple(
            str(item) for item in cast(dict[str, Any], unit["candidate_summaries"])
        )
        carryover_ids = config.carryover_candidates_by_workload[workload]
        if set(carryover_ids) != set(candidate_ids) - {"fused"}:
            raise ValueError(f"V5 carryover candidate set changed for {unit_id}")
        carryover = assess_carryover(
            rows,
            candidate_ids=candidate_ids,
            carryover_candidate_ids=carryover_ids,
            tolerance_fraction=config.carryover_tolerance_fraction,
            confidence_level=config.confidence_level,
            bootstrap_repetitions=config.bootstrap_repetitions,
            bootstrap_seed=config.bootstrap_seed,
            minimum_pairs=config.minimum_carryover_pairs,
        )
        claims = authorize_paired_claims(
            rows,
            candidate_ids=candidate_ids,
            baseline_id="fused",
            carryover_candidate_ids=carryover_ids,
            tie_fraction=config.tie_threshold_fraction,
            confidence_level=config.confidence_level,
            bootstrap_repetitions=config.bootstrap_repetitions,
            bootstrap_seed=config.bootstrap_seed,
            minimum_blocks=config.minimum_claim_blocks,
        )
        inference_ready = all(
            item["classification"] != "INSUFFICIENT_PAIRS" for item in carryover
        ) and all(item["conclusion"] != "INSUFFICIENT_BLOCKS" for item in claims)
        oracle_set = _authorized_oracle_set(claims) if inference_ready else None
        unit_results.append(
            {
                "unit_id": unit_id,
                "workload": workload,
                "carryover_assessments": carryover,
                "paired_claims": claims,
                "inference_protocol_ready": inference_ready,
                "authorized_oracle_set": oracle_set,
                "model_label_authorized": oracle_set is not None,
            }
        )

    global_integrity = cast(dict[str, bool], legacy["global_gates"])
    structural_pass = all(global_integrity.values())
    inference_ready = all(bool(item["inference_protocol_ready"]) for item in unit_results)
    eligible_count = sum(bool(item["model_label_authorized"]) for item in unit_results)
    gates = {
        "structural_integrity": structural_pass,
        "pollution_safe_inference_ready": inference_ready,
        "minimum_model_eligible_units": (eligible_count >= config.minimum_model_eligible_units),
        "development_boundary_preserved": (
            not bool(summary["paper_performance_evidence"])
            and not bool(summary["heldout_optimizer_evidence"])
            and not bool(summary["optimizer_selection_evaluated"])
        ),
    }
    result: dict[str, object] = {
        "schema_version": 1,
        "status": (
            "PASS_V5_POLLUTION_SAFE_CALIBRATION"
            if all(gates.values())
            else "FAIL_V5_POLLUTION_SAFE_CALIBRATION_RETAIN"
        ),
        "run_id": summary["run_id"],
        "analyzed_at_utc": datetime.now(UTC).isoformat(),
        "gate_checks": gates,
        "model_eligible_unit_count": eligible_count,
        "unit_results": unit_results,
        "legacy_stability_diagnostics": legacy,
        "raw_measurements_preserved": True,
        "paper_performance_evidence": False,
        "heldout_optimizer_evidence": False,
        "scientific_boundary": config.scientific_boundary,
        "inference_config": asdict(config),
    }
    _atomic_json(run_dir / "v5_inference.json", result)
    return result
