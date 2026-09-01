"""Frozen evaluation gates for formal record-lineage scalability measurements."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.paired_claims import stratified_paired_bootstrap_ci
from trustaero.experiments.record_lineage_pilot import DIRECT, RECORD


class RecordLineageEvaluationError(RuntimeError):
    """Raised when a formal run is incomplete or differs from frozen inputs."""


@dataclass(frozen=True, slots=True)
class RecordLineageEvaluationConfig:
    """Predeclared integrity and timing-stability gates."""

    results_dir: str
    measurement_results_dir: str
    measurement_config_path: str
    expected_row_counts: tuple[int, ...]
    expected_blocks_per_unit: int
    confidence_level: float
    bootstrap_repetitions: int
    bootstrap_seed: int
    maximum_position_median_spread_fraction: float
    maximum_half_ratio_drift_fraction: float
    maximum_bytes_per_edge: float
    expected_artifact_encoding: str = "duckdb_digest_v3"

    def __post_init__(self) -> None:
        if not self.expected_row_counts or len(set(self.expected_row_counts)) != len(
            self.expected_row_counts
        ):
            raise ValueError("Expected record-lineage scales must be nonempty and unique")
        if self.expected_blocks_per_unit < 20:
            raise ValueError("Formal record-lineage evaluation requires at least 20 blocks")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("Confidence level must be in (0, 1)")
        if self.bootstrap_repetitions < 1000:
            raise ValueError("Formal bootstrap requires at least 1000 repetitions")
        if self.maximum_position_median_spread_fraction <= 0.0:
            raise ValueError("Position-spread limit must be positive")
        if self.maximum_half_ratio_drift_fraction <= 0.0:
            raise ValueError("Half-ratio drift limit must be positive")
        if self.maximum_bytes_per_edge < 64.0:
            if not (
                self.expected_artifact_encoding == "ordinal_bound_v4"
                and self.maximum_bytes_per_edge >= 32.0
            ):
                raise ValueError("Storage gate is inconsistent with the encoding")
        if self.expected_artifact_encoding not in {
            "duckdb_digest_v3",
            "ordinal_bound_v4",
        }:
            raise ValueError("Formal record-lineage encoding is unsupported")


def load_record_lineage_evaluation_config(
    path: str | Path,
) -> RecordLineageEvaluationConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["expected_row_counts"] = tuple(int(value) for value in payload["expected_row_counts"])
    return RecordLineageEvaluationConfig(**payload)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _position_spread(
    rows: list[dict[str, Any]],
    variant: str,
) -> dict[str, Any]:
    """Check that a variant is not materially advantaged by first/second place."""

    matching = [row for row in rows if row["variant"] == variant]
    values: dict[int, list[float]] = {}
    for row in matching:
        values.setdefault(int(row["order_position"]), []).append(float(row["total_latency_ms"]))
    medians = {
        str(position): statistics.median(latencies)
        for position, latencies in sorted(values.items())
    }
    counts = {str(position): len(latencies) for position, latencies in values.items()}
    median_values = list(medians.values())
    return {
        "position_medians_ms": medians,
        "position_counts": counts,
        "maximum_over_minimum_fraction": max(median_values) / min(median_values) - 1.0,
        "count_imbalance": max(counts.values()) - min(counts.values()),
    }


def evaluate_record_lineage_unit(
    rows: list[dict[str, Any]],
    config: RecordLineageEvaluationConfig,
) -> dict[str, Any]:
    """Evaluate one scale from complete direct/record paired blocks."""

    if not rows:
        raise RecordLineageEvaluationError("Record-lineage unit contains no rows")
    row_count = int(rows[0]["row_count"])
    blocks = sorted({int(row["block_index"]) for row in rows})
    paired_ratios: list[float] = []
    ratios_by_order: dict[str, list[float]] = {}
    complete_blocks = True
    for block in blocks:
        block_rows = [row for row in rows if int(row["block_index"]) == block]
        by_variant = {str(row["variant"]): row for row in block_rows}
        if set(by_variant) != {DIRECT, RECORD} or len(block_rows) != 2:
            complete_blocks = False
            continue
        ratio = float(by_variant[RECORD]["total_latency_ms"]) / float(
            by_variant[DIRECT]["total_latency_ms"]
        )
        paired_ratios.append(ratio)
        order = str(block_rows[0]["order_id"])
        ratios_by_order.setdefault(order, []).append(ratio)
    if not paired_ratios:
        raise RecordLineageEvaluationError(f"No complete pairs for n={row_count}")

    midpoint = len(paired_ratios) // 2
    first_half = statistics.median(paired_ratios[:midpoint])
    second_half = statistics.median(paired_ratios[midpoint:])
    half_drift = abs(first_half / second_half - 1.0)
    interval = stratified_paired_bootstrap_ci(
        ratios_by_order,
        confidence_level=config.confidence_level,
        repetitions=config.bootstrap_repetitions,
        seed=config.bootstrap_seed + row_count,
    )
    position = {variant: _position_spread(rows, variant) for variant in (DIRECT, RECORD)}
    maximum_position_spread = max(
        float(item["maximum_over_minimum_fraction"]) for item in position.values()
    )
    maximum_position_imbalance = max(int(item["count_imbalance"]) for item in position.values())
    record_rows = [row for row in rows if row["variant"] == RECORD]
    result_digests = {str(row["result_digest"]) for row in rows}
    edge_digests = {str(row["lineage_edge_digest"]) for row in record_rows}
    edge_counts = {int(row["lineage_edge_count"]) for row in record_rows}
    artifact_sizes = {int(row["lineage_artifact_bytes"]) for row in record_rows}
    bytes_per_edge = max(artifact_sizes) / row_count
    gates = {
        "expected_block_count": len(blocks) == config.expected_blocks_per_unit,
        "every_block_is_complete": complete_blocks,
        "one_result_digest": len(result_digests) == 1,
        "all_evidence_verified": all(
            str(row["lineage_verified"]).lower() == "true" for row in record_rows
        ),
        "one_edge_digest": len(edge_digests) == 1,
        "edge_count_matches_output": edge_counts == {row_count},
        # The binary header contains execution_id, whose textual length may
        # change between block 9 and block 10. Exact total-file equality is
        # therefore not a semantic or storage-stability property. The frozen
        # protocol correctly gates the maximum bytes per edge instead.
        "compact_storage": bytes_per_edge <= config.maximum_bytes_per_edge,
        "balanced_positions": maximum_position_imbalance <= 1,
        "position_stability": (
            maximum_position_spread <= config.maximum_position_median_spread_fraction
        ),
        "first_second_half_stability": (half_drift <= config.maximum_half_ratio_drift_fraction),
    }
    median_ratio = statistics.median(paired_ratios)
    return {
        "row_count": row_count,
        "paired_block_count": len(paired_ratios),
        "median_record_over_direct_ratio": median_ratio,
        "median_record_over_direct_overhead_percent": (median_ratio - 1.0) * 100.0,
        "paired_bootstrap_confidence_interval": {
            "method": "order_stratified_paired_bootstrap_median_ratio_v1",
            "level": config.confidence_level,
            "lower": interval[0],
            "upper": interval[1],
            "repetitions": config.bootstrap_repetitions,
            "seed": config.bootstrap_seed + row_count,
        },
        "direct_median_ms": statistics.median(
            float(row["total_latency_ms"]) for row in rows if row["variant"] == DIRECT
        ),
        "record_median_ms": statistics.median(
            float(row["total_latency_ms"]) for row in record_rows
        ),
        "capture_median_ms": statistics.median(
            float(row["lineage_capture_latency_ms"]) for row in record_rows
        ),
        "verification_median_ms": statistics.median(
            float(row["lineage_verification_latency_ms"]) for row in record_rows
        ),
        "artifact_bytes": max(artifact_sizes),
        "artifact_size_range_bytes": max(artifact_sizes) - min(artifact_sizes),
        "bytes_per_edge": bytes_per_edge,
        "first_half_median_ratio": first_half,
        "second_half_median_ratio": second_half,
        "first_second_half_drift_fraction": half_drift,
        "maximum_position_median_spread_fraction": maximum_position_spread,
        "position_diagnostics": position,
        "gates": gates,
        "passed": all(gates.values()),
    }


def evaluate_record_lineage_formal(
    config: RecordLineageEvaluationConfig,
    *,
    project_root: Path,
    measurement_run_dir: Path,
    config_path: Path,
) -> Path:
    """Validate one frozen formal run and emit its independently bound verdict."""

    root = project_root.resolve()
    run_dir = measurement_run_dir.resolve()
    evaluator_commit, evaluator_dirty = _git_state(root)
    run_config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    expected_config = json.loads(
        (root / config.measurement_config_path).read_text(encoding="utf-8")
    )
    if run_config != expected_config:
        raise RecordLineageEvaluationError(
            "Measurement run does not match the frozen formal configuration"
        )
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    actual_counts = sorted({int(row["row_count"]) for row in rows})
    findings = [
        evaluate_record_lineage_unit(
            [row for row in rows if int(row["row_count"]) == row_count],
            config,
        )
        for row_count in config.expected_row_counts
        if row_count in actual_counts
    ]
    gates = {
        "measurement_integrity_passed": (
            summary.get("status") == "PASS_RECORD_LINEAGE_PILOT_INTEGRITY"
        ),
        "formal_role": (summary.get("experiment_role") == "record_lineage_scalability_formal"),
        "expected_artifact_encoding": (
            summary.get("artifact_encoding") == config.expected_artifact_encoding
        ),
        "clean_measurement_commit": environment.get("git_dirty") is False,
        "expected_scales": actual_counts == sorted(config.expected_row_counts),
        "all_findings_present": len(findings) == len(config.expected_row_counts),
        "all_unit_gates_passed": all(item["passed"] for item in findings),
        "measurement_did_not_self_authorize": (summary.get("paper_performance_evidence") is False),
    }
    passed = all(gates.values())
    payload = {
        "status": (
            "PASS_RECORD_LINEAGE_FORMAL_SCALABILITY_EVIDENCE"
            if passed
            else "FAIL_RECORD_LINEAGE_FORMAL_SCALABILITY_RETAIN"
        ),
        "measurement_run": str(run_dir.relative_to(root)).replace("\\", "/"),
        "measurement_commit": environment.get("commit_hash"),
        "evaluator_commit": evaluator_commit,
        "evaluator_git_dirty": evaluator_dirty,
        "measurement_config_path": config.measurement_config_path,
        "measurement_config_sha256": _sha256(root / config.measurement_config_path),
        "evaluation_config_path": str(config_path.relative_to(root)).replace("\\", "/"),
        "evaluation_config_sha256": _sha256(config_path),
        "measurement_summary_sha256": _sha256(run_dir / "summary.json"),
        "measurement_csv_sha256": _sha256(run_dir / "measurements.csv"),
        "unit_findings": findings,
        "gates": gates,
        "failed_gates": [name for name, value in gates.items() if not value],
        "record_lineage_performance_evidence_authorized": passed,
        "scientific_boundary": [
            "The result covers the single-source, single-STRING-key record fragment.",
            "Join, Aggregate, SpatialJoin, composite keys, and non-STRING keys remain unsupported.",
            "The direct and record variants execute the same validated query result.",
            "A PASS authorizes the measured overhead and storage observations, "
            "not broader lineage claims.",
        ],
    }
    output = root / config.results_dir / run_dir.name
    _atomic_json(output / "evaluation.json", payload)
    return output
