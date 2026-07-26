"""Build a content-addressed cross-workload evidence matrix.

This module normalizes already accepted measurements. It does not train or run
an optimizer, and it refuses to relabel development partitions as held-out
evidence. The resulting ratios make candidate-boundary reversals comparable
without comparing raw milliseconds across unrelated workloads.
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file
from trustaero.experiments.tpch_audit import tpch_git_state
from trustaero.reproducibility import audit_source_freeze


class BenchmarkEvidenceError(RuntimeError):
    """Raised when a source cannot support the requested scientific claim."""


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    workload_id: str
    domain: str
    query_family: str
    evidence_scope: str
    reference_candidate_id: str
    accepted_oracle_set: tuple[str, ...]
    summary_path: str
    summary_sha256: str
    acceptance_path: str
    acceptance_sha256: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.workload_id,
                self.domain,
                self.query_family,
                self.evidence_scope,
                self.reference_candidate_id,
                self.accepted_oracle_set,
            )
        ):
            raise ValueError("evidence-source labels cannot be empty")
        for path_text in (self.summary_path, self.acceptance_path):
            path = Path(path_text)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("evidence paths must stay inside the project")
        for digest in (self.summary_sha256, self.acceptance_sha256):
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("evidence bindings must be lowercase SHA-256")
        if len(self.accepted_oracle_set) != len(set(self.accepted_oracle_set)):
            raise ValueError("accepted Oracle candidates must be unique")


@dataclass(frozen=True, slots=True)
class BenchmarkEvidenceConfig:
    output_dir: str
    tie_threshold_fraction: float
    require_clean_git: bool
    sources: tuple[EvidenceSource, ...]

    def __post_init__(self) -> None:
        output = Path(self.output_dir)
        if output.is_absolute() or ".." in output.parts or not self.output_dir:
            raise ValueError("evidence output must stay inside the project")
        if not 0.0 <= self.tie_threshold_fraction < 1.0:
            raise ValueError("tie threshold must be in [0, 1)")
        if not self.require_clean_git:
            raise ValueError("paper evidence aggregation requires a clean source commit")
        if len(self.sources) < 2:
            raise ValueError("cross-workload evidence requires at least two sources")
        ids = tuple(item.workload_id for item in self.sources)
        if len(ids) != len(set(ids)):
            raise ValueError("evidence workload IDs must be unique")


def load_benchmark_evidence_config(path: Path | str) -> BenchmarkEvidenceConfig:
    """Parse a strict, content-addressed evidence configuration."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ValueError("benchmark evidence config must contain a source list")
    sources: list[EvidenceSource] = []
    for item in payload["sources"]:
        if not isinstance(item, dict) or not isinstance(item.get("accepted_oracle_set"), list):
            raise ValueError("each evidence source must contain an accepted Oracle list")
        sources.append(
            EvidenceSource(
                workload_id=str(item["workload_id"]),
                domain=str(item["domain"]),
                query_family=str(item["query_family"]),
                evidence_scope=str(item["evidence_scope"]),
                reference_candidate_id=str(item["reference_candidate_id"]),
                accepted_oracle_set=tuple(str(value) for value in item["accepted_oracle_set"]),
                summary_path=str(item["summary_path"]),
                summary_sha256=str(item["summary_sha256"]),
                acceptance_path=str(item["acceptance_path"]),
                acceptance_sha256=str(item["acceptance_sha256"]),
            )
        )
    return BenchmarkEvidenceConfig(
        output_dir=str(payload["output_dir"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
        sources=tuple(sources),
    )


def _load_bound_json(root: Path, relative_path: str, expected_sha256: str) -> dict[str, Any]:
    path = root / relative_path
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise BenchmarkEvidenceError(f"Frozen evidence binding changed: {relative_path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BenchmarkEvidenceError(f"Evidence file must contain an object: {relative_path}")
    return value


def _is_formally_authorized(acceptance: dict[str, Any]) -> bool:
    return (
        acceptance.get("formal_paper_experiment_authorized") is True
        or acceptance.get("formal_performance_experiment_authorized") is True
    )


def _candidate_rows(
    source: EvidenceSource,
    summary: dict[str, Any],
    *,
    tie_threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_candidates = summary.get("candidate_summaries")
    if not isinstance(raw_candidates, dict) or len(raw_candidates) < 2:
        raise BenchmarkEvidenceError(f"{source.workload_id} has no candidate matrix")
    if source.reference_candidate_id not in raw_candidates:
        raise BenchmarkEvidenceError(f"{source.workload_id} lacks its reference candidate")

    medians: dict[str, float] = {}
    for candidate_id, values in raw_candidates.items():
        if not isinstance(values, dict):
            raise BenchmarkEvidenceError(f"Malformed candidate: {candidate_id}")
        median = float(values.get("median_ms", 0.0))
        if median <= 0.0:
            raise BenchmarkEvidenceError(f"Candidate has no positive median: {candidate_id}")
        medians[str(candidate_id)] = median
    oracle_ms = min(medians.values())
    oracle_set = sorted(
        candidate_id
        for candidate_id, median in medians.items()
        if median <= oracle_ms * (1.0 + tie_threshold)
    )
    if oracle_set != sorted(source.accepted_oracle_set):
        raise BenchmarkEvidenceError(
            f"{source.workload_id} pooled medians disagree with its accepted Oracle set"
        )
    reference_ms = medians[source.reference_candidate_id]
    rows: list[dict[str, Any]] = []
    for candidate_id, values in raw_candidates.items():
        median = medians[str(candidate_id)]
        p95 = float(values.get("p95_ms", median))
        peak_memory = int(values.get("peak_buffer_memory_bytes", 0))
        spill = int(values.get("peak_temp_directory_bytes", 0))
        rows.append(
            {
                "workload_id": source.workload_id,
                "domain": source.domain,
                "query_family": source.query_family,
                "evidence_scope": source.evidence_scope,
                "candidate_id": str(candidate_id),
                "is_reference": str(candidate_id) == source.reference_candidate_id,
                "is_in_oracle_set": str(candidate_id) in oracle_set,
                "median_ms": median,
                "p95_ms": p95,
                "pooled_median_over_reference_ratio": median / reference_ms,
                "pooled_median_regret_vs_oracle_percent": (median / oracle_ms - 1.0) * 100.0,
                "peak_buffer_memory_mib": peak_memory / 1048576.0,
                "spill_bytes": spill,
            }
        )
    workload = {
        "workload_id": source.workload_id,
        "domain": source.domain,
        "query_family": source.query_family,
        "evidence_scope": source.evidence_scope,
        "candidate_count": len(rows),
        "reference_candidate_id": source.reference_candidate_id,
        "reference_median_ms": reference_ms,
        "oracle_median_ms": oracle_ms,
        "oracle_set_within_tie_band": oracle_set,
        "pooled_median_oracle_opportunity_vs_reference": reference_ms / oracle_ms,
        "pooled_median_reference_regret_percent": (reference_ms / oracle_ms - 1.0) * 100.0,
        "alternative_boundary_within_tie_band": any(
            item != source.reference_candidate_id for item in oracle_set
        ),
        "reference_outside_oracle_set": source.reference_candidate_id not in oracle_set,
    }
    return rows, workload


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def build_benchmark_evidence_matrix(
    config: BenchmarkEvidenceConfig,
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Normalize accepted measurements without making an optimizer claim."""

    root = project_root.resolve()
    if audit_source_freeze(root).status != "READY":
        raise BenchmarkEvidenceError("benchmark evidence requires source READY")
    commit, dirty = tpch_git_state(root)
    if dirty:
        raise BenchmarkEvidenceError("benchmark evidence requires a clean worktree")

    candidate_rows: list[dict[str, Any]] = []
    workloads: list[dict[str, Any]] = []
    source_bindings: list[dict[str, Any]] = []
    for source in config.sources:
        summary = _load_bound_json(root, source.summary_path, source.summary_sha256)
        acceptance = _load_bound_json(root, source.acceptance_path, source.acceptance_sha256)
        if (
            summary.get("status") != "PASS"
            or acceptance.get("status") != "PASS"
            or summary.get("paper_performance_evidence") is not True
            or acceptance.get("paper_performance_evidence") is not True
            or not _is_formally_authorized(acceptance)
            or acceptance.get("heldout_optimizer_evidence") is not False
            or acceptance.get("optimizer_selection_evaluated") is True
            or summary.get("optimizer_selection_evaluated") is True
        ):
            raise BenchmarkEvidenceError(
                f"{source.workload_id} does not satisfy the evidence boundary"
            )
        rows, workload = _candidate_rows(
            source,
            summary,
            tie_threshold=config.tie_threshold_fraction,
        )
        candidate_rows.extend(rows)
        workloads.append(workload)
        source_bindings.append(asdict(source))

    opportunities = [
        float(item["pooled_median_oracle_opportunity_vs_reference"]) for item in workloads
    ]
    alternatives = [item for item in workloads if item["alternative_boundary_within_tie_band"]]
    strict_reversals = [item for item in workloads if item["reference_outside_oracle_set"]]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "source_commit": commit,
        "paper_performance_evidence": True,
        "derived_cross_workload_evidence": True,
        "heldout_optimizer_evidence": False,
        "optimizer_selection_evaluated": False,
        "tie_threshold_fraction": config.tie_threshold_fraction,
        "cross_workload_normalization": (
            "ratio_of_source_reported_candidate_medians; accepted source-specific "
            "Oracle sets are independently cross-checked"
        ),
        "workload_count": len(workloads),
        "candidate_count": len(candidate_rows),
        "workloads_with_alternative_boundary_in_tie_band_count": len(alternatives),
        "workloads_with_alternative_boundary_in_tie_band": [
            item["workload_id"] for item in alternatives
        ],
        "workloads_with_reference_outside_oracle_set_count": len(strict_reversals),
        "workloads_with_reference_outside_oracle_set": [
            item["workload_id"] for item in strict_reversals
        ],
        "reference_within_tie_band_rate": sum(
            item["reference_candidate_id"] in item["oracle_set_within_tie_band"]
            for item in workloads
        )
        / len(workloads),
        "max_pooled_median_reference_regret_percent": max(
            float(item["pooled_median_reference_regret_percent"]) for item in workloads
        ),
        "geometric_pooled_median_oracle_opportunity_vs_reference": math.exp(
            sum(math.log(value) for value in opportunities) / len(opportunities)
        ),
        "workloads": workloads,
        "source_bindings": source_bindings,
        "scientific_boundary": (
            "This matrix normalizes accepted measurements. It demonstrates that the best "
            "legal boundary can vary, but it neither trains nor evaluates an optimizer."
        ),
    }
    output = root / config.output_dir
    _atomic_json(output / "summary.json", payload)
    _write_csv(output / "candidates.csv", candidate_rows)
    lines = [
        "# Cross-workload governed candidate evidence",
        "",
        "This is derived paper-performance evidence, not optimizer evaluation.",
        "",
        "| Workload | Query family | Candidates | Reference regret | Oracle set |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for item in workloads:
        lines.append(
            f"| {item['workload_id']} | {item['query_family']} | {item['candidate_count']} | "
            f"{item['pooled_median_reference_regret_percent']:.2f}% | "
            f"{', '.join(item['oracle_set_within_tie_band'])} |"
        )
    lines.extend(
        [
            "",
            f"- Workloads: `{len(workloads)}`",
            f"- Alternative boundary in 3% tie band: `{len(alternatives)}`",
            f"- Reference outside Oracle set: `{len(strict_reversals)}`",
            "- Held-out optimizer evidence: `False`",
            "- Optimizer selection evaluated: `False`",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return payload
