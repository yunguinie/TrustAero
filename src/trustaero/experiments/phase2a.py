"""Phase 2A: controlled data and actual DuckDB physical-plan observations.

This is a pre-optimizer experiment. It compares two result-equivalent execution
strategy prototypes and refuses to call SQL text differences physical-plan
differences unless their DuckDB fingerprints actually differ.
"""

from __future__ import annotations

import csv
import hashlib
import json
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

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.synthetic import (
    SyntheticDataConfig,
    SyntheticWorkloadStats,
    generate_synthetic_workload,
)


@dataclass(frozen=True)
class Phase2AConfig:
    """Repeatable settings for controlled physical-plan smoke experiments."""

    results_dir: str
    workloads: tuple[SyntheticDataConfig, ...]
    warmup_runs: int = 2
    measured_runs: int = 10

    def __post_init__(self) -> None:
        if not self.workloads:
            raise ValueError("Phase 2A requires at least one workload")
        if self.warmup_runs < 0 or self.measured_runs < 1:
            raise ValueError("warmup_runs must be nonnegative and measured_runs positive")


@dataclass(frozen=True)
class Phase2AStrategyResult:
    """One strategy/workload measurement written to the Phase 2A CSV."""

    run_id: str
    commit_hash: str
    workload_id: str
    strategy: str
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
    physical_operator_names: tuple[str, ...]
    max_actual_cardinality: int
    total_rows_scanned: int
    cold_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float


_PREDICATE = """
events.policy_allowed
AND events.event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
AND events.event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
AND 111.045 * sqrt(
  power(events.latitude - 40.0, 2)
  + power((events.longitude - 116.3) * cos(radians(40.0)), 2)
) <= 20.0
""".strip()

_FUSED_SQL = f"""
SELECT events.event_id, dimension.severity_label
FROM synthetic_events AS events
INNER JOIN severity_dim AS dimension
  ON events.join_key = dimension.join_key
WHERE {_PREDICATE}
ORDER BY events.event_id
""".strip()

_MATERIALIZED_SQL = f"""
WITH filtered AS MATERIALIZED (
  SELECT events.event_id, events.join_key
  FROM synthetic_events AS events
  WHERE {_PREDICATE}
)
SELECT filtered.event_id, dimension.severity_label
FROM filtered
INNER JOIN severity_dim AS dimension
  ON filtered.join_key = dimension.join_key
ORDER BY filtered.event_id
""".strip()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
        f"SELECT COUNT(*) FROM synthetic_events AS events WHERE {_PREDICATE}"
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
    strategy: str,
    sql: str,
    warmup_runs: int,
    measured_runs: int,
    plan_dir: Path,
) -> tuple[Phase2AStrategyResult, tuple[tuple[Any, ...], ...]]:
    """Measure one strategy and save its actual analyzed DuckDB plan."""

    cold_latency_ms, cold_rows = _execute(connection, sql)
    for _ in range(warmup_runs):
        _execute(connection, sql)
    latencies: list[float] = []
    last_rows = cold_rows
    for _ in range(measured_runs):
        latency_ms, last_rows = _execute(connection, sql)
        latencies.append(latency_ms)

    observation = observe_duckdb_plan(connection, sql, analyze=True)
    plan_path = plan_dir / f"{stats.workload_id}-{strategy}.json"
    plan_path.write_text(observation.plan_json + "\n", encoding="utf-8")
    result = Phase2AStrategyResult(
        run_id=run_id,
        commit_hash=commit_hash,
        workload_id=stats.workload_id,
        strategy=strategy,
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
        physical_operator_names=observation.operator_names,
        max_actual_cardinality=observation.max_intermediate_cardinality,
        total_rows_scanned=sum(observation.rows_scanned),
        cold_latency_ms=cold_latency_ms,
        median_latency_ms=statistics.median(latencies),
        p95_latency_ms=_percentile_95(latencies),
        min_latency_ms=min(latencies),
        max_latency_ms=max(latencies),
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


def _environment(commit_hash: str) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for package in ("trustaero", "duckdb", "pydantic"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "unknown"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "commit_hash": commit_hash,
        "packages": packages,
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

    connection = duckdb.connect(":memory:")
    try:
        for workload in config.workloads:
            stats = generate_synthetic_workload(connection, workload)
            fused, fused_rows = _measure_strategy(
                connection,
                run_id=run_id,
                commit_hash=commit_hash,
                stats=stats,
                strategy="fused",
                sql=_FUSED_SQL,
                warmup_runs=config.warmup_runs,
                measured_runs=config.measured_runs,
                plan_dir=plan_dir,
            )
            materialized, materialized_rows = _measure_strategy(
                connection,
                run_id=run_id,
                commit_hash=commit_hash,
                stats=stats,
                strategy="materialized_cte",
                sql=_MATERIALIZED_SQL,
                warmup_runs=config.warmup_runs,
                measured_runs=config.measured_runs,
                plan_dir=plan_dir,
            )
            equivalent = fused_rows == materialized_rows
            fused = Phase2AStrategyResult(**{**asdict(fused), "result_equivalent": equivalent})
            materialized = Phase2AStrategyResult(
                **{**asdict(materialized), "result_equivalent": equivalent}
            )
            rows.extend((fused, materialized))
            workload_summaries.append(
                {
                    "workload_id": workload.workload_id,
                    "result_equivalent": equivalent,
                    "physical_plans_distinct": (
                        fused.physical_plan_fingerprint != materialized.physical_plan_fingerprint
                    ),
                    "winner": (
                        fused.strategy
                        if fused.median_latency_ms <= materialized.median_latency_ms
                        else materialized.strategy
                    ),
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
        "strategy_count": 2,
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
        json.dumps(_environment(commit_hash), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_dir
