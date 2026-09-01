"""Paired integrity and overhead pilot for the record-lineage V1 fragment."""

from __future__ import annotations

import copy
import csv
import itertools
import json
import os
import platform
import random
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import (
    RecordLineageCaptureSpec,
    TableBindings,
    compile_record_lineage_plan,
    execute_compact_record_lineage_with_connection,
    execute_database_digest_record_lineage_with_connection,
    execute_ordinal_record_lineage_with_connection,
    execute_record_lineage_with_connection,
    execute_with_connection,
    verify_compact_record_lineage_artifact,
    verify_database_digest_record_lineage_artifact,
    verify_ordinal_record_lineage_artifact,
    verify_record_lineage_artifact,
)
from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.validator.service import validate

DIRECT = "direct_query"
RECORD = "record_lineage"
VARIANTS = (DIRECT, RECORD)


@dataclass(frozen=True, slots=True)
class RecordLineagePilotConfig:
    """Frozen dimensions for a non-publication record-lineage pilot."""

    results_dir: str
    row_counts: tuple[int, ...]
    warmup_rounds: int
    repetitions_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    require_clean_git: bool
    experiment_role: str
    artifact_encoding: str = "object_json_v1"

    def __post_init__(self) -> None:
        if not self.results_dir or Path(self.results_dir).is_absolute():
            raise ValueError("Record-lineage results directory must be repository-relative")
        if not self.row_counts or len(self.row_counts) != len(set(self.row_counts)):
            raise ValueError("Record-lineage row counts must be nonempty and unique")
        if any(value < 1 for value in self.row_counts):
            raise ValueError("Record-lineage row counts must be positive")
        if self.warmup_rounds < 1 or self.repetitions_per_permutation < 1:
            raise ValueError("Record-lineage pilot requires warmup and measured rounds")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 256:
            raise ValueError("Record-lineage DuckDB controls are invalid")
        if self.experiment_role not in {
            "record_lineage_integrity_pilot",
            "record_lineage_scalability_formal",
        }:
            raise ValueError("Record-lineage pilot role is invalid")
        if self.artifact_encoding not in {
            "object_json_v1",
            "compact_binary_v2",
            "duckdb_digest_v3",
            "ordinal_bound_v4",
        }:
            raise ValueError("Record-lineage artifact encoding is invalid")

    @property
    def blocks_per_unit(self) -> int:
        return len(tuple(itertools.permutations(VARIANTS))) * self.repetitions_per_permutation


def load_record_lineage_pilot_config(
    path: str | Path,
) -> RecordLineagePilotConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["row_counts"] = tuple(int(value) for value in payload["row_counts"])
    return RecordLineagePilotConfig(**payload)


def record_lineage_orders(
    repetitions_per_permutation: int,
    *,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    """Balance both variants in both positions, then shuffle complete blocks."""

    orders = list(itertools.permutations(VARIANTS)) * repetitions_per_permutation
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _load_record_plan(
    root: Path,
) -> tuple[ValidatedLogicalPlan, InMemoryCatalog]:
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(
            json.loads(
                (root / "examples/catalogs/minimal_catalog.json").read_text(encoding="utf-8")
            )
        )
    )
    policy_payload = json.loads(
        (root / "examples/policies/research_policy.json").read_text(encoding="utf-8")
    )
    policy_payload["rules"][0]["obligations"] = [
        {
            "obligation_type": "LINEAGE_CAPTURE",
            "parameters": {"level": "record"},
        }
    ]
    policy = PolicySet.model_validate(policy_payload)
    raw_plan = json.loads(
        (root / "examples/plans/accept_earthquakes.json").read_text(encoding="utf-8")
    )
    response = validate(copy.deepcopy(raw_plan), policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise ValueError("Record-lineage pilot plan did not validate and rewrite")
    return response.validated_plan, catalog


def _create_data(connection: Any, row_count: int) -> None:
    connection.execute("DROP TABLE IF EXISTS earthquake_events")
    connection.execute(
        f"""
        CREATE TABLE earthquake_events AS
        SELECT
            'eq-' || lpad(CAST(i AS VARCHAR), 12, '0') AS event_id,
            TIMESTAMPTZ '2026-01-01 00:00:00+00'
                + CAST(i % 86400 AS BIGINT) * INTERVAL 1 SECOND AS event_time,
            30.0 + CAST(i % 1000 AS DOUBLE) / 1000.0 AS latitude,
            -120.0 + CAST(i % 1000 AS DOUBLE) / 1000.0 AS longitude,
            1.0 + CAST(i % 70 AS DOUBLE) / 10.0 AS magnitude
        FROM range({row_count}) AS source(i)
        """
    )


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _environment(
    commit: str,
    dirty: bool,
    config: RecordLineagePilotConfig,
) -> dict[str, object]:
    try:
        duckdb_version = metadata.version("duckdb")
    except metadata.PackageNotFoundError:
        duckdb_version = "not-installed"
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "commit_hash": commit,
        "git_dirty": dirty,
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "duckdb_version": duckdb_version,
        "duckdb_threads": config.duckdb_threads,
        "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
        "gpu_acceleration": False,
    }


def _median(rows: list[dict[str, object]], field: str) -> float:
    values: list[float] = []
    for row in rows:
        value = row[field]
        if not isinstance(value, (int, float)):
            raise TypeError(f"Record-lineage numeric field is invalid: {field}")
        values.append(float(value))
    return statistics.median(values)


def _integer(row: dict[str, object], field: str) -> int:
    value = row[field]
    if not isinstance(value, int):
        raise TypeError(f"Record-lineage integer field is invalid: {field}")
    return value


def run_record_lineage_pilot(
    config: RecordLineagePilotConfig,
    *,
    project_root: Path,
    config_path: Path,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Execute the frozen paired pilot and retain only compact evidence summaries."""

    import duckdb

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Record-lineage pilot requires a clean committed Git snapshot")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / config.results_dir / run_id
    _atomic_json(output / "config.json", asdict(config))
    _atomic_json(output / "environment.json", _environment(commit, dirty, config))

    plan, catalog = _load_record_plan(root)
    spec = RecordLineageCaptureSpec("earthquakes", ("event_id",))
    all_rows: list[dict[str, object]] = []
    total_blocks = len(config.row_counts) * config.blocks_per_unit
    completed_blocks = 0
    started = time.perf_counter()

    for unit_index, row_count in enumerate(config.row_counts):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(f"SET threads = {config.duckdb_threads}")
            connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
            _create_data(connection, row_count)
            compiled = compile_record_lineage_plan(
                plan,
                catalog,
                TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
                spec=spec,
            )

            for warmup in range(config.warmup_rounds):
                execute_with_connection(compiled.query, connection)
                # Warm the exact record-lineage implementation being measured.
                # Otherwise V2/V3 would unfairly time their first execution
                # after only the legacy object implementation had been warmed.
                warmup_execution_id = f"warmup-{row_count}-{warmup}"
                if config.artifact_encoding == "ordinal_bound_v4":
                    execute_ordinal_record_lineage_with_connection(
                        compiled,
                        connection,
                        execution_id=warmup_execution_id,
                    )
                elif config.artifact_encoding == "duckdb_digest_v3":
                    execute_database_digest_record_lineage_with_connection(
                        compiled,
                        connection,
                        execution_id=warmup_execution_id,
                    )
                elif config.artifact_encoding == "compact_binary_v2":
                    execute_compact_record_lineage_with_connection(
                        compiled,
                        connection,
                        execution_id=warmup_execution_id,
                    )
                else:
                    execute_record_lineage_with_connection(
                        compiled,
                        connection,
                        execution_id=warmup_execution_id,
                    )

            unit_rows: list[dict[str, object]] = []
            orders = record_lineage_orders(
                config.repetitions_per_permutation,
                seed=config.order_seed + unit_index,
            )
            for block_index, order in enumerate(orders):
                block_rows: list[dict[str, object]] = []
                for position, variant in enumerate(order):
                    execution_id = f"record-pilot-{row_count}-{block_index}-{position}"
                    call_started = time.perf_counter()
                    if variant == DIRECT:
                        result = execute_with_connection(compiled.query, connection)
                        total_ms = (time.perf_counter() - call_started) * 1000.0
                        row = {
                            "row_count": row_count,
                            "block_index": block_index,
                            "order_position": position,
                            "order_id": "->".join(order),
                            "variant": variant,
                            "result_digest": result.result_digest,
                            "total_latency_ms": total_ms,
                            "lineage_capture_latency_ms": 0.0,
                            "lineage_verification_latency_ms": 0.0,
                            "lineage_edge_count": 0,
                            "lineage_artifact_bytes": 0,
                            "lineage_edge_digest": "",
                            "lineage_verified": False,
                        }
                    else:
                        if config.artifact_encoding == "ordinal_bound_v4":
                            ordinal_result = execute_ordinal_record_lineage_with_connection(
                                compiled,
                                connection,
                                execution_id=execution_id,
                            )
                            total_ms = (time.perf_counter() - call_started) * 1000.0
                            verify_started = time.perf_counter()
                            verification = verify_ordinal_record_lineage_artifact(
                                plan,
                                columns=ordinal_result.query_result.columns,
                                rows=ordinal_result.query_result.rows,
                                spec=spec,
                                evidence=ordinal_result.lineage.evidence,
                                artifact=ordinal_result.lineage.artifact,
                            )
                            result_digest = ordinal_result.query_result.result_digest
                            capture_ms = ordinal_result.lineage.latency_ms
                            edge_count = ordinal_result.lineage.artifact.edge_count
                            artifact_bytes = len(ordinal_result.lineage.artifact.binary_payload())
                            edge_digest = ordinal_result.lineage.evidence.edge_digest
                        elif config.artifact_encoding == "duckdb_digest_v3":
                            database_result = (
                                execute_database_digest_record_lineage_with_connection(
                                    compiled,
                                    connection,
                                    execution_id=execution_id,
                                )
                            )
                            total_ms = (time.perf_counter() - call_started) * 1000.0
                            verify_started = time.perf_counter()
                            verification = verify_database_digest_record_lineage_artifact(
                                plan,
                                columns=database_result.query_result.columns,
                                rows=database_result.query_result.rows,
                                spec=spec,
                                evidence=database_result.lineage.evidence,
                                artifact=database_result.lineage.artifact,
                            )
                            result_digest = database_result.query_result.result_digest
                            capture_ms = database_result.lineage.latency_ms
                            edge_count = database_result.lineage.artifact.edge_count
                            artifact_bytes = len(database_result.lineage.artifact.binary_payload())
                            edge_digest = database_result.lineage.evidence.edge_digest
                        elif config.artifact_encoding == "compact_binary_v2":
                            compact = execute_compact_record_lineage_with_connection(
                                compiled,
                                connection,
                                execution_id=execution_id,
                            )
                            total_ms = (time.perf_counter() - call_started) * 1000.0
                            verify_started = time.perf_counter()
                            verification = verify_compact_record_lineage_artifact(
                                plan,
                                columns=compact.query_result.columns,
                                rows=compact.query_result.rows,
                                spec=spec,
                                evidence=compact.lineage.evidence,
                                artifact=compact.lineage.artifact,
                            )
                            result_digest = compact.query_result.result_digest
                            capture_ms = compact.lineage.latency_ms
                            edge_count = compact.lineage.artifact.edge_count
                            artifact_bytes = len(compact.lineage.artifact.binary_payload())
                            edge_digest = compact.lineage.evidence.edge_digest
                        else:
                            object_result = execute_record_lineage_with_connection(
                                compiled,
                                connection,
                                execution_id=execution_id,
                            )
                            total_ms = (time.perf_counter() - call_started) * 1000.0
                            verify_started = time.perf_counter()
                            verification = verify_record_lineage_artifact(
                                plan,
                                columns=object_result.query_result.columns,
                                rows=object_result.query_result.rows,
                                spec=spec,
                                evidence=object_result.lineage.evidence,
                                artifact=object_result.lineage.artifact,
                            )
                            result_digest = object_result.query_result.result_digest
                            capture_ms = object_result.lineage.latency_ms
                            edge_count = len(object_result.lineage.artifact.edges)
                            artifact_bytes = len(
                                json.dumps(
                                    object_result.lineage.artifact.payload(),
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode()
                            )
                            edge_digest = object_result.lineage.evidence.edge_digest
                        verification_ms = (time.perf_counter() - verify_started) * 1000.0
                        if not verification.satisfied:
                            raise ValueError(
                                f"Record-lineage verification failed at {row_count} rows"
                            )
                        row = {
                            "row_count": row_count,
                            "block_index": block_index,
                            "order_position": position,
                            "order_id": "->".join(order),
                            "variant": variant,
                            "result_digest": result_digest,
                            "total_latency_ms": total_ms,
                            "lineage_capture_latency_ms": (capture_ms),
                            "lineage_verification_latency_ms": verification_ms,
                            "lineage_edge_count": edge_count,
                            "lineage_artifact_bytes": artifact_bytes,
                            "lineage_edge_digest": (edge_digest),
                            "lineage_verified": True,
                        }
                    block_rows.append(row)
                if len({str(row["result_digest"]) for row in block_rows}) != 1:
                    raise ValueError(f"Paired result changed at {row_count} rows")
                unit_rows.extend(block_rows)
                completed_blocks += 1
                if progress is not None:
                    progress(
                        completed_blocks,
                        total_blocks,
                        f"n{row_count} block={block_index + 1}",
                        time.perf_counter() - started,
                    )
            _atomic_json(
                output / "units" / f"n{row_count}.json",
                {
                    "row_count": row_count,
                    "measurement_count": len(unit_rows),
                    "all_result_digests_equal": len(
                        {str(row["result_digest"]) for row in unit_rows}
                    )
                    == 1,
                    "all_record_evidence_verified": all(
                        bool(row["lineage_verified"])
                        for row in unit_rows
                        if row["variant"] == RECORD
                    ),
                    "direct_median_ms": _median(
                        [row for row in unit_rows if row["variant"] == DIRECT],
                        "total_latency_ms",
                    ),
                    "record_median_ms": _median(
                        [row for row in unit_rows if row["variant"] == RECORD],
                        "total_latency_ms",
                    ),
                    "capture_median_ms": _median(
                        [row for row in unit_rows if row["variant"] == RECORD],
                        "lineage_capture_latency_ms",
                    ),
                    "verification_median_ms": _median(
                        [row for row in unit_rows if row["variant"] == RECORD],
                        "lineage_verification_latency_ms",
                    ),
                    "artifact_bytes": max(
                        _integer(row, "lineage_artifact_bytes") for row in unit_rows
                    ),
                },
            )
            all_rows.extend(unit_rows)
        finally:
            connection.close()

    _write_csv(output / "measurements.csv", all_rows)
    unit_summaries = [
        json.loads((output / "units" / f"n{rows}.json").read_text(encoding="utf-8"))
        for rows in config.row_counts
    ]
    summary = {
        "status": "PASS_RECORD_LINEAGE_PILOT_INTEGRITY",
        "experiment_role": config.experiment_role,
        "unit_count": len(unit_summaries),
        "measurement_count": len(all_rows),
        "all_results_equivalent": all(item["all_result_digests_equal"] for item in unit_summaries),
        "all_record_evidence_verified": all(
            item["all_record_evidence_verified"] for item in unit_summaries
        ),
        "unit_summaries": unit_summaries,
        "paper_performance_evidence": False,
        "record_lineage_fragment": (
            "single-source unique-unmasked-output-key Filter/Mask/Project/Sort"
        ),
        "artifact_encoding": config.artifact_encoding,
        "unsupported_fragments": ["Join", "Aggregate", "SpatialJoin"],
    }
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_id},
    )
    return output
