"""Phase 2A: controlled data and approved DuckDB-plan observations.

This is still a pre-optimizer experiment: it does not claim that TrustAero can
already choose the fastest candidate.  It does ensure that every measured SQL
strategy comes from an ``ApprovedPhysicalPlan`` bound to one validated logical
plan, and it verifies real DuckDB plan differences instead of trusting SQL text.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import capture_source_lineage, observe_duckdb_plan
from trustaero.experiments.physical_sql import compile_phase2_strategy
from trustaero.experiments.synthetic import (
    SyntheticDataConfig,
    SyntheticWorkloadStats,
    generate_synthetic_workload,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate


@dataclass(frozen=True)
class Phase2AConfig:
    """Repeatable settings for controlled physical-plan smoke experiments."""

    results_dir: str
    workloads: tuple[SyntheticDataConfig, ...]
    warmup_runs: int = 2
    measured_runs: int = 10
    duckdb_threads: int = 4
    duckdb_memory_limit_mb: int = 4096
    materialization_targets: tuple[str, ...] = ("op-event-project",)
    source_lineage: bool = False

    def __post_init__(self) -> None:
        if not self.workloads:
            raise ValueError("Phase 2A requires at least one workload")
        if self.warmup_runs < 0 or self.measured_runs < 1:
            raise ValueError("warmup_runs must be nonnegative and measured_runs positive")
        if self.duckdb_threads < 1:
            raise ValueError("duckdb_threads must be positive")
        if self.duckdb_memory_limit_mb < 128:
            raise ValueError("duckdb_memory_limit_mb must be at least 128")
        if not self.materialization_targets:
            raise ValueError("Phase 2 requires at least one materialization target")


@dataclass(frozen=True)
class Phase2AStrategyResult:
    """One strategy/workload measurement written to the Phase 2A CSV."""

    run_id: str
    commit_hash: str
    workload_id: str
    strategy: str
    logical_plan_id: str
    approved_physical_plan_id: str
    strategy_id: str
    row_count: int
    temporal_selectivity: float
    spatial_selectivity: float
    policy_selectivity: float
    join_match_rate: float
    hot_key_fraction: float
    filtered_row_count: int
    output_row_count: int
    result_digest: str
    result_equivalent: bool
    physical_plan_fingerprint: str
    physical_plan_representative: str
    is_fingerprint_representative: bool
    physical_operator_names: tuple[str, ...]
    max_actual_cardinality: int
    total_rows_scanned: int
    cold_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    lineage_level: str
    lineage_source_count: int
    median_lineage_latency_ms: float
    median_governed_latency_ms: float


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_phase2_experiment_plan(
    *,
    source_lineage: bool = False,
    mask_event_id: bool = False,
) -> ValidatedLogicalPlan:
    """Build the small governed query shared by every controlled workload.

    Data values vary between workloads, but schema, policy, query semantics,
    and snapshot bindings remain fixed.  Therefore candidate latency changes
    can be attributed to controlled statistics rather than authorization drift.
    """

    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(
            {
                "schema_version": "1.0",
                "datasets": [
                    {
                        "dataset_id": "synthetic_events",
                        "versions": ["synthetic-v1"],
                        "default_version": "synthetic-v1",
                        "fields": [
                            {
                                "name": "event_id",
                                "data_type": "string",
                                "roles": ["identifier"],
                            },
                            {
                                "name": "event_time",
                                "data_type": "datetime",
                                "roles": ["temporal"],
                            },
                            {"name": "latitude", "data_type": "float", "roles": ["spatial"]},
                            {
                                "name": "longitude",
                                "data_type": "float",
                                "roles": ["spatial"],
                            },
                            {"name": "policy_allowed", "data_type": "boolean", "roles": []},
                            {"name": "join_key", "data_type": "string", "roles": []},
                            {"name": "magnitude", "data_type": "float", "roles": []},
                        ],
                        "spatial": {
                            "latitude_field": "latitude",
                            "longitude_field": "longitude",
                            "crs": "EPSG:4326",
                        },
                        "temporal_field": "event_time",
                    },
                    {
                        "dataset_id": "severity_dim",
                        "versions": ["synthetic-v1"],
                        "default_version": "synthetic-v1",
                        "fields": [
                            {"name": "dimension_key", "data_type": "string", "roles": []},
                            {"name": "severity_label", "data_type": "string", "roles": []},
                        ],
                        "spatial": None,
                        "temporal_field": None,
                    },
                ],
            }
        )
    )
    obligations: list[dict[str, object]] = []
    if mask_event_id:
        obligations.append(
            {
                "obligation_type": "MASK",
                "parameters": {"fields": ["event_id"], "method": "hash"},
            }
        )
    if source_lineage:
        obligations.append(
            {"obligation_type": "LINEAGE_CAPTURE", "parameters": {"level": "source"}}
        )
    policies = PolicySet.model_validate(
        {
            "schema_version": "1.0",
            "policy_set_id": "phase2a-policy-set",
            "policy_snapshot": "phase2a-policy-v1",
            "rules": [
                {
                    "policy_id": "P-PHASE2A-JOIN",
                    "policy_version": "1",
                    "subject_roles": ["researcher"],
                    "purposes": ["research"],
                    "actions": ["join"],
                    "resources": ["synthetic_events", "severity_dim"],
                    "decision": "PERMIT",
                    "obligations": obligations,
                    "reason": "Controlled synthetic join is permitted for evaluation.",
                }
            ],
        }
    )
    raw_plan = {
        "schema_version": "1.0",
        "plan_id": "phase2a-controlled-query",
        "request_context": {
            "subject": {"subject_id": "phase2a-runner", "role": "researcher", "attributes": {}},
            "purpose": "research",
            "action": "join",
            "query_time_window": None,
        },
        "requested_output": {
            "fields": ["event_id", "severity_label"],
            "export": {"requested": False, "destination": None, "format": None},
            "lineage_level": "none",
        },
        "operators": [
            {
                "operator_type": "ScanSource",
                "operator_id": "op-events",
                "inputs": [],
                "dataset": "synthetic_events",
                "snapshot": "synthetic-v1",
            },
            {
                "operator_type": "TemporalFilter",
                "operator_id": "op-temporal",
                "inputs": ["op-events"],
                "field": "event_time",
                "start": "2026-06-01T00:00:00+00:00",
                "end": "2026-06-02T00:00:00+00:00",
            },
            {
                "operator_type": "SpatialFilter",
                "operator_id": "op-spatial",
                "inputs": ["op-temporal"],
                "center": [40.0, 116.3],
                "radius_km": 20.0,
                "crs": "EPSG:4326",
            },
            {
                "operator_type": "Filter",
                "operator_id": "op-policy",
                "inputs": ["op-spatial"],
                "expression": {
                    "expression_type": "comparison",
                    "operator": "eq",
                    "left": {"expression_type": "field", "field": "policy_allowed"},
                    "right": {
                        "expression_type": "literal",
                        "data_type": "boolean",
                        "value": True,
                    },
                },
            },
            {
                "operator_type": "Project",
                "operator_id": "op-event-project",
                "inputs": ["op-policy"],
                "fields": ["event_id", "join_key"],
            },
            {
                "operator_type": "ScanSource",
                "operator_id": "op-dimension",
                "inputs": [],
                "dataset": "severity_dim",
                "snapshot": "synthetic-v1",
            },
            {
                "operator_type": "Join",
                "operator_id": "op-join",
                "inputs": ["op-event-project", "op-dimension"],
                "left_field": "join_key",
                "right_field": "dimension_key",
                "join_type": "inner",
            },
            {
                "operator_type": "Project",
                "operator_id": "op-output",
                "inputs": ["op-join"],
                "fields": ["event_id", "severity_label"],
            },
        ],
        "output_operator": "op-output",
    }
    response = validate(raw_plan, policies, catalog)
    expected_status = (
        ValidationStatus.REWRITE if source_lineage or mask_event_id else ValidationStatus.ACCEPT
    )
    if response.status != expected_status or response.validated_plan is None:
        diagnostics = ", ".join(item.code.value for item in response.diagnostics)
        raise RuntimeError(f"Phase 2A logical plan validation failed: {diagnostics}")
    return response.validated_plan


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip()


def _percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def _result_digest(rows: tuple[tuple[Any, ...], ...]) -> str:
    payload = json.dumps(rows, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _execute(connection: Any, sql: str) -> tuple[float, tuple[tuple[Any, ...], ...]]:
    started = time.perf_counter()
    rows = tuple(tuple(row) for row in connection.execute(sql).fetchall())
    return (time.perf_counter() - started) * 1000.0, rows


def _filtered_row_count(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM synthetic_events AS events
        WHERE events.event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
          AND events.event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
          AND 111.045 * sqrt(
            power(events.latitude - 40.0, 2)
            + power((events.longitude - 116.3) * cos(radians(40.0)), 2)
          ) <= 20.0
          AND events.policy_allowed
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("DuckDB did not return the filtered intermediate cardinality")
    return int(row[0])


def _measure_strategy(
    connection: Any,
    *,
    run_id: str,
    commit_hash: str,
    stats: SyntheticWorkloadStats,
    logical_plan: ValidatedLogicalPlan,
    strategy: str,
    logical_plan_id: str,
    approved_physical_plan_id: str,
    strategy_id: str,
    sql: str,
    warmup_runs: int,
    measured_runs: int,
    plan_dir: Path,
) -> tuple[Phase2AStrategyResult, tuple[tuple[Any, ...], ...]]:
    """Measure one strategy and save its actual analyzed DuckDB plan."""

    def execute_once() -> tuple[float, float, int, tuple[tuple[Any, ...], ...]]:
        """Measure query and source-lineage costs without conflating them."""

        query_latency_ms, result_rows = _execute(connection, sql)
        lineage = capture_source_lineage(
            logical_plan,
            execution_id=f"{run_id}:{stats.workload_id}:{strategy_id}",
            result_id=_result_digest(result_rows),
        )
        return query_latency_ms, lineage.latency_ms, lineage.source_count, result_rows

    cold_latency_ms, _, _, cold_rows = execute_once()
    for _ in range(warmup_runs):
        execute_once()
    latencies: list[float] = []
    lineage_latencies: list[float] = []
    last_rows = cold_rows
    lineage_source_count = 0
    for _ in range(measured_runs):
        latency_ms, lineage_latency_ms, lineage_source_count, last_rows = execute_once()
        latencies.append(latency_ms)
        lineage_latencies.append(lineage_latency_ms)

    observation = observe_duckdb_plan(connection, sql, analyze=True)
    plan_path = plan_dir / f"{stats.workload_id}-{strategy}.json"
    plan_path.write_text(observation.plan_json + "\n", encoding="utf-8")
    result = Phase2AStrategyResult(
        run_id=run_id,
        commit_hash=commit_hash,
        workload_id=stats.workload_id,
        strategy=strategy,
        logical_plan_id=logical_plan_id,
        approved_physical_plan_id=approved_physical_plan_id,
        strategy_id=strategy_id,
        row_count=stats.row_count,
        temporal_selectivity=stats.temporal_selectivity,
        spatial_selectivity=stats.spatial_selectivity,
        policy_selectivity=stats.policy_selectivity,
        join_match_rate=stats.join_match_rate,
        hot_key_fraction=stats.hot_key_fraction,
        filtered_row_count=_filtered_row_count(connection),
        output_row_count=len(last_rows),
        result_digest=_result_digest(last_rows),
        result_equivalent=False,
        physical_plan_fingerprint=observation.fingerprint,
        physical_plan_representative=approved_physical_plan_id,
        is_fingerprint_representative=True,
        physical_operator_names=observation.operator_names,
        max_actual_cardinality=observation.max_intermediate_cardinality,
        total_rows_scanned=sum(observation.rows_scanned),
        cold_latency_ms=cold_latency_ms,
        median_latency_ms=statistics.median(latencies),
        p95_latency_ms=_percentile_95(latencies),
        min_latency_ms=min(latencies),
        max_latency_ms=max(latencies),
        lineage_level=("source" if logical_plan.lineage_requirements else "none"),
        lineage_source_count=lineage_source_count,
        median_lineage_latency_ms=statistics.median(lineage_latencies),
        median_governed_latency_ms=statistics.median(
            [
                query_latency + lineage_latency
                for query_latency, lineage_latency in zip(latencies, lineage_latencies, strict=True)
            ]
        ),
    )
    return result, last_rows


def _write_csv(path: Path, rows: tuple[Phase2AStrategyResult, ...]) -> None:
    fieldnames = list(Phase2AStrategyResult.__annotations__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in rows:
            row = asdict(result)
            row["physical_operator_names"] = "|".join(result.physical_operator_names)
            writer.writerow(row)


def _environment(commit_hash: str, config: Phase2AConfig) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for package in ("trustaero", "duckdb", "pydantic"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "unknown"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "commit_hash": commit_hash,
        "packages": packages,
        # These are explicitly applied to DuckDB before any table generation
        # or timing, so later runs do not inherit workstation-global defaults.
        "duckdb": {
            "threads": config.duckdb_threads,
            "memory_limit_mb": config.duckdb_memory_limit_mb,
            "gpu_acceleration": False,
        },
    }


def run_phase2a(config: Phase2AConfig) -> Path:
    """Run controlled workloads and require result/physical-plan checks."""

    import duckdb

    root = _repo_root()
    run_id = _run_id()
    commit_hash = _git_commit(root)
    output_dir = root / config.results_dir / run_id
    plan_dir = output_dir / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    rows: list[Phase2AStrategyResult] = []
    workload_summaries: list[dict[str, Any]] = []
    logical_plan = build_phase2_experiment_plan(source_lineage=config.source_lineage)
    candidates = generate_duckdb_candidates(
        logical_plan,
        materialization_targets=config.materialization_targets,
    )
    if any(candidate.unimplemented_backend_features for candidate in candidates):
        raise RuntimeError("Phase 2A cannot execute candidates with unimplemented backend features")

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        for workload in config.workloads:
            stats = generate_synthetic_workload(connection, workload)
            measured: list[tuple[Phase2AStrategyResult, tuple[tuple[Any, ...], ...]]] = []
            for candidate in candidates:
                label = (
                    "materialized_cte"
                    if candidate.strategy.strategy_id == "materialize-after-op-event-project"
                    else candidate.strategy.strategy_id
                )
                measured.append(
                    _measure_strategy(
                        connection,
                        run_id=run_id,
                        commit_hash=commit_hash,
                        stats=stats,
                        logical_plan=logical_plan,
                        strategy=label,
                        logical_plan_id=logical_plan.logical_plan_id,
                        approved_physical_plan_id=candidate.physical_plan_id,
                        strategy_id=candidate.strategy.strategy_id,
                        sql=compile_phase2_strategy(candidate),
                        warmup_runs=config.warmup_runs,
                        measured_runs=config.measured_runs,
                        plan_dir=plan_dir,
                    )
                )

            equivalent = all(item_rows == measured[0][1] for _, item_rows in measured)
            fingerprint_groups: dict[str, list[str]] = {}
            for result, _ in measured:
                fingerprint_groups.setdefault(result.physical_plan_fingerprint, []).append(
                    result.strategy_id
                )
            representative_by_fingerprint = {
                fingerprint: strategy_ids[0]
                for fingerprint, strategy_ids in fingerprint_groups.items()
            }
            workload_results: list[Phase2AStrategyResult] = []
            for result, _ in measured:
                representative_strategy = representative_by_fingerprint[
                    result.physical_plan_fingerprint
                ]
                representative_plan_id = next(
                    item.approved_physical_plan_id
                    for item, _ in measured
                    if item.strategy_id == representative_strategy
                )
                workload_results.append(
                    Phase2AStrategyResult(
                        **{
                            **asdict(result),
                            "result_equivalent": equivalent,
                            "physical_plan_representative": representative_plan_id,
                            "is_fingerprint_representative": (
                                result.strategy_id == representative_strategy
                            ),
                        }
                    )
                )
            rows.extend(workload_results)
            # DuckDB may erase a nominal strategy difference (for example by
            # projection pushdown). Selecting between duplicate fingerprints
            # would reward timing noise, so only representatives enter ranking.
            representative_results = [
                item for item in workload_results if item.is_fingerprint_representative
            ]
            winner = min(representative_results, key=lambda item: item.median_governed_latency_ms)
            runner_up = sorted(
                representative_results, key=lambda item: item.median_governed_latency_ms
            )[1]
            workload_summaries.append(
                {
                    "workload_id": workload.workload_id,
                    "logical_plan_id": logical_plan.logical_plan_id,
                    "approved_candidate_ids": [
                        item.approved_physical_plan_id for item in workload_results
                    ],
                    "result_equivalent": equivalent,
                    "physical_plans_distinct": len(fingerprint_groups) == len(candidates),
                    "unique_physical_plan_count": len(fingerprint_groups),
                    "deduplicated_candidate_count": len(candidates) - len(fingerprint_groups),
                    "fingerprint_groups": list(fingerprint_groups.values()),
                    # This is a descriptive observation, not a significance
                    # claim.  Repeated independent runs are needed before a
                    # small median gap can be called a performance reversal.
                    "observed_median_winner": winner.strategy_id,
                    "median_gap_fraction": (
                        runner_up.median_governed_latency_ms - winner.median_governed_latency_ms
                    )
                    / winner.median_governed_latency_ms,
                }
            )
    finally:
        connection.close()

    result_rows = tuple(rows)
    _write_csv(output_dir / "cases.csv", result_rows)
    summary = {
        "all_results_equivalent": all(row.result_equivalent for row in result_rows),
        "all_physical_plans_distinct": all(
            item["physical_plans_distinct"] for item in workload_summaries
        ),
        "strategy_count": len(candidates),
        "all_candidates_approved": bool(candidates),
        "source_lineage_enabled": config.source_lineage,
        "logical_plan_id": logical_plan.logical_plan_id,
        "workload_count": len(config.workloads),
        "controlled_statistics": [
            "row_count",
            "temporal_selectivity",
            "spatial_selectivity",
            "policy_selectivity",
            "join_match_rate",
            "hot_key_fraction",
        ],
        "workloads": workload_summaries,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "config.json").write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "environment.json").write_text(
        json.dumps(_environment(commit_hash, config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_dir
