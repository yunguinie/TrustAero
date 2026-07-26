"""Frozen evaluation for paired TrustAero system-scalability measurements.

The evaluator authorizes evidence validity, not a preferred performance
outcome.  A high overhead remains a publishable observation when all semantic,
pairing, stability, and provenance gates pass.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.experiments.paired_claims import stratified_paired_bootstrap_ci
from trustaero.experiments.system_scalability import LAYER_IDS

DIRECT_LAYER = "direct_database_equivalent_sql"
COMPLETE_LAYER = "complete_trustaero_with_certificate"


class SystemScalabilityEvaluationError(RuntimeError):
    """Raised when bound formal artifacts are incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class SystemScalabilityEvaluationConfig:
    """Predeclared evidence and stability gates."""

    results_dir: str
    measurement_results_dir: str
    measurement_config_path: str
    expected_unit_ids: tuple[str, ...]
    expected_blocks_per_unit: int
    confidence_level: float
    bootstrap_repetitions: int
    bootstrap_seed: int
    maximum_position_median_spread_fraction: float
    maximum_half_ratio_drift_fraction: float
    maximum_spilled_unit_count: int
    required_certificate_status: str

    def __post_init__(self) -> None:
        if not self.expected_unit_ids or len(set(self.expected_unit_ids)) != len(
            self.expected_unit_ids
        ):
            raise ValueError("Expected formal unit IDs must be nonempty and unique")
        if self.expected_blocks_per_unit < 20:
            raise ValueError("Formal evaluation requires at least 20 paired blocks")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("Confidence level must be in (0, 1)")
        if self.bootstrap_repetitions < 1000:
            raise ValueError("Formal bootstrap requires at least 1000 repetitions")
        if self.maximum_position_median_spread_fraction <= 0.0:
            raise ValueError("Position-spread limit must be positive")
        if self.maximum_half_ratio_drift_fraction <= 0.0:
            raise ValueError("Half-drift limit must be positive")
        if self.maximum_spilled_unit_count < 0:
            raise ValueError("Maximum spilled units cannot be negative")
        if self.required_certificate_status != "PARTIAL":
            raise ValueError("Current execution certificate boundary must remain PARTIAL")


def load_system_scalability_evaluation_config(
    path: str | Path,
) -> SystemScalabilityEvaluationConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["expected_unit_ids"] = tuple(payload["expected_unit_ids"])
    return SystemScalabilityEvaluationConfig(**payload)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _median(rows: list[dict[str, Any]], field: str) -> float:
    return statistics.median(float(row[field]) for row in rows)


def _position_spread(rows: list[dict[str, Any]], layer_id: str) -> dict[str, Any]:
    """Measure whether a layer changes materially with execution position."""

    layer_rows = [row for row in rows if row["layer_id"] == layer_id]
    by_position: dict[int, list[float]] = {}
    for row in layer_rows:
        by_position.setdefault(int(row["order_position"]), []).append(
            float(row["end_to_end_latency_ms"])
        )
    medians = {
        str(position): statistics.median(values) for position, values in sorted(by_position.items())
    }
    values = list(medians.values())
    spread = max(values) / min(values) - 1.0
    counts = {str(position): len(values) for position, values in by_position.items()}
    return {
        "position_medians_ms": medians,
        "position_counts": counts,
        "maximum_over_minimum_fraction": spread,
        "count_imbalance": max(counts.values()) - min(counts.values()),
    }


def evaluate_unit_measurements(
    rows: list[dict[str, Any]],
    config: SystemScalabilityEvaluationConfig,
    *,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate one dataset-scale unit using complete paired blocks."""

    if not rows:
        raise SystemScalabilityEvaluationError("Formal unit contains no measurements")
    unit_id = str(rows[0]["unit_id"])
    blocks = sorted({int(row["block_index"]) for row in rows})
    complete_blocks = True
    for block in blocks:
        block_rows = [row for row in rows if int(row["block_index"]) == block]
        if len(block_rows) != len(LAYER_IDS) or {str(row["layer_id"]) for row in block_rows} != set(
            LAYER_IDS
        ):
            complete_blocks = False

    result_digests = {str(row["result_digest"]) for row in rows}
    lineage_edge_digests = {
        str(row["lineage_edge_digest"]) for row in rows if row.get("lineage_edge_digest")
    }
    certificate_rows = [row for row in rows if row["layer_id"] == COMPLETE_LAYER]
    certificate_statuses = {str(row["certificate_status"]) for row in certificate_rows}

    paired_ratios: list[float] = []
    for block in blocks:
        block_rows = [row for row in rows if int(row["block_index"]) == block]
        by_layer = {str(row["layer_id"]): row for row in block_rows}
        if set(by_layer) != set(LAYER_IDS):
            continue
        direct = float(by_layer[DIRECT_LAYER]["end_to_end_latency_ms"])
        complete = float(by_layer[COMPLETE_LAYER]["end_to_end_latency_ms"])
        paired_ratios.append(complete / direct)

    if not paired_ratios:
        raise SystemScalabilityEvaluationError(f"No complete paired ratios for {unit_id}")
    midpoint = len(paired_ratios) // 2
    first_half = statistics.median(paired_ratios[:midpoint])
    second_half = statistics.median(paired_ratios[midpoint:])
    half_drift = abs(first_half / second_half - 1.0)
    interval = stratified_paired_bootstrap_ci(
        {"balanced_paired_blocks": paired_ratios},
        confidence_level=config.confidence_level,
        repetitions=config.bootstrap_repetitions,
        seed=config.bootstrap_seed,
    )
    position = {layer_id: _position_spread(rows, layer_id) for layer_id in LAYER_IDS}
    maximum_position_spread = max(
        float(value["maximum_over_minimum_fraction"]) for value in position.values()
    )
    maximum_position_count_imbalance = max(
        int(value["count_imbalance"]) for value in position.values()
    )
    complete_rows = [row for row in rows if row["layer_id"] == COMPLETE_LAYER]
    component_medians = {
        field: _median(complete_rows, field)
        for field in (
            "policy_validation_latency_ms",
            "planner_latency_ms",
            "database_execution_latency_ms",
            "lineage_capture_latency_ms",
            "certificate_verification_latency_ms",
        )
    }
    gates = {
        "expected_block_count": len(blocks) == config.expected_blocks_per_unit,
        "every_block_has_all_layers": complete_blocks,
        "one_result_digest": len(result_digests) == 1,
        "one_source_lineage_edge_digest": len(lineage_edge_digests) == 1,
        "certificate_count": (len(certificate_rows) == config.expected_blocks_per_unit),
        "certificate_status": certificate_statuses == {config.required_certificate_status},
        "balanced_positions": maximum_position_count_imbalance <= 1,
        "position_stability": (
            maximum_position_spread <= config.maximum_position_median_spread_fraction
        ),
        "first_second_half_stability": (half_drift <= config.maximum_half_ratio_drift_fraction),
    }
    return {
        "unit_id": unit_id,
        "input_rows": int(rows[0]["input_rows"]),
        "output_rows": int(rows[0]["output_rows"]),
        "paired_block_count": len(paired_ratios),
        "median_complete_over_direct_ratio": statistics.median(paired_ratios),
        "median_complete_over_direct_overhead_percent": (statistics.median(paired_ratios) - 1.0)
        * 100.0,
        "paired_bootstrap_confidence_interval": {
            "method": "paired_bootstrap_median_ratio_v1",
            "level": config.confidence_level,
            "lower": interval[0],
            "upper": interval[1],
            "repetitions": config.bootstrap_repetitions,
            "seed": config.bootstrap_seed,
        },
        "first_half_median_ratio": first_half,
        "second_half_median_ratio": second_half,
        "first_second_half_drift_fraction": half_drift,
        "maximum_position_median_spread_fraction": maximum_position_spread,
        "maximum_position_count_imbalance": maximum_position_count_imbalance,
        "position_diagnostics": position,
        "complete_layer_component_medians_ms": component_medians,
        "profile": profile,
        "gates": gates,
        "passed": all(gates.values()),
    }


def evaluate_system_scalability(
    config: SystemScalabilityEvaluationConfig,
    *,
    project_root: Path,
    measurement_run_dir: Path,
    config_path: Path,
) -> Path:
    """Validate a frozen measurement run and write one acceptance record."""

    root = project_root.resolve()
    run_dir = measurement_run_dir.resolve()
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    run_config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    frozen_measurement_config = json.loads(
        (root / config.measurement_config_path).read_text(encoding="utf-8")
    )
    if run_config != frozen_measurement_config:
        raise SystemScalabilityEvaluationError(
            "Measurement run does not match the frozen formal config"
        )

    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    actual_units = sorted({str(row["unit_id"]) for row in rows})
    profiles = {str(item["unit_id"]): item for item in summary.get("profiles", [])}
    findings = [
        evaluate_unit_measurements(
            [row for row in rows if row["unit_id"] == unit_id],
            config,
            profile=profiles[unit_id],
        )
        for unit_id in config.expected_unit_ids
        if unit_id in profiles
    ]
    spilled_units = sum(int(item["profile"]["temporary_spill_bytes"]) > 0 for item in findings)
    gates = {
        "measurement_integrity_passed": (
            summary.get("status") == "PASS_SYSTEM_SCALABILITY_MEASUREMENT_INTEGRITY"
        ),
        "clean_measurement_commit": environment.get("git_dirty") is False,
        "expected_units": actual_units == sorted(config.expected_unit_ids),
        "all_unit_findings_present": len(findings) == len(config.expected_unit_ids),
        "all_unit_gates_passed": all(item["passed"] for item in findings),
        "maximum_spilled_unit_count": (spilled_units <= config.maximum_spilled_unit_count),
        "measurement_did_not_self_authorize": (summary.get("paper_performance_evidence") is False),
    }
    status = (
        "PASS_SYSTEM_SCALABILITY_FORMAL_SOURCE_LINEAGE_EVIDENCE"
        if all(gates.values())
        else "FAIL_SYSTEM_SCALABILITY_FORMAL_SOURCE_LINEAGE_RETAIN"
    )
    payload = {
        "status": status,
        "measurement_run": str(run_dir.relative_to(root)).replace("\\", "/"),
        "measurement_commit": environment.get("commit_hash"),
        "measurement_config_path": config.measurement_config_path,
        "measurement_config_sha256": _sha256(root / config.measurement_config_path),
        "evaluation_config_path": str(config_path.relative_to(root)).replace("\\", "/"),
        "evaluation_config_sha256": _sha256(config_path),
        "measurement_summary_sha256": _sha256(run_dir / "summary.json"),
        "measurement_csv_sha256": _sha256(run_dir / "measurements.csv"),
        "unit_findings": findings,
        "spilled_unit_count": spilled_units,
        "gates": gates,
        "failed_gates": [name for name, passed in gates.items() if not passed],
        "source_lineage_performance_evidence_authorized": all(gates.values()),
        "record_lineage_performance_evidence_authorized": False,
        "optimizer_claim_reopened": False,
        "scientific_boundary": [
            "The direct layer executes identical policy-compliant SQL while "
            "bypassing the TrustAero control path.",
            "The evidence covers source-snapshot lineage, not record-level lineage.",
            "Certificate verification remains PARTIAL by design.",
            "The result covers only the explicitly configured units: "
            + ", ".join(config.expected_unit_ids)
            + ".",
            "No result from this evaluator refits or retunes the frozen optimizer.",
        ],
    }
    output = root / config.results_dir / run_dir.name
    _atomic_json(output / "evaluation.json", payload)
    return output
