"""Resumable real-data infrastructure pilot with visible progress.

This module deliberately measures only the canonical governed plan produced by
TrustAero.  It does not compare optimizer candidates and therefore cannot be
used to claim optimizer speedup.  Its job is to verify that real-data timing,
cardinality, physical-plan, environment, and checkpoint artifacts are reliable
before a paper protocol is frozen.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_real_data_slice_artifacts
from trustaero.execution import (
    compile_validated_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _create_trusted_views,
    _execute_governed_case,
    _load_json,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.validator.service import validate

SUPPORTED_WORKLOADS = ("bts", "nyc_tlc")


@dataclass(frozen=True, slots=True)
class RealDataPilotConfig:
    """Frozen controls for a non-paper real-data infrastructure pilot."""

    results_dir: str
    workloads: tuple[str, ...]
    sample_rows: tuple[int, ...]
    warmup_runs: int = 1
    measured_runs: int = 5
    duckdb_threads: int = 4
    duckdb_memory_limit_mb: int = 4096
    unit_order_seed: int = 20260718
    require_clean_git: bool = False
    scientific_label: str = "real_data_infrastructure_pilot_not_paper_performance_evidence"

    def __post_init__(self) -> None:
        if not self.workloads or not self.sample_rows:
            raise ValueError("workloads and sample_rows cannot be empty")
        if len(set(self.workloads)) != len(self.workloads):
            raise ValueError("workloads cannot contain duplicates")
        unknown = set(self.workloads) - set(SUPPORTED_WORKLOADS)
        if unknown:
            raise ValueError(f"unsupported real-data workloads: {sorted(unknown)}")
        if any(value < 1 for value in self.sample_rows):
            raise ValueError("sample_rows must be positive")
        if len(set(self.sample_rows)) != len(self.sample_rows):
            raise ValueError("sample_rows cannot contain duplicates")
        if self.warmup_runs < 0 or self.measured_runs < 1:
            raise ValueError("invalid warmup/measured run counts")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("invalid DuckDB resource controls")
        if self.scientific_label != (
            "real_data_infrastructure_pilot_not_paper_performance_evidence"
        ):
            raise ValueError("pilot scientific boundary label cannot be weakened")


@dataclass(frozen=True, slots=True)
class RealDataPilotMeasurement:
    """One canonical-plan execution; digest work is outside the timer."""

    unit_id: str
    workload: str
    sample_rows: int
    repeat_index: int
    client_materialization_latency_ms: float
    output_row_count: int
    semantic_result_digest: str


def load_real_data_pilot_config(path: Path | str) -> RealDataPilotConfig:
    """Load and validate the compact JSON protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("real-data pilot config must be a JSON object")
    return RealDataPilotConfig(
        results_dir=str(payload["results_dir"]),
        workloads=tuple(str(item) for item in payload["workloads"]),
        sample_rows=tuple(int(item) for item in payload["sample_rows"]),
        warmup_runs=int(payload.get("warmup_runs", 1)),
        measured_runs=int(payload.get("measured_runs", 5)),
        duckdb_threads=int(payload.get("duckdb_threads", 4)),
        duckdb_memory_limit_mb=int(payload.get("duckdb_memory_limit_mb", 4096)),
        unit_order_seed=int(payload.get("unit_order_seed", 20260718)),
        require_clean_git=bool(payload.get("require_clean_git", False)),
        scientific_label=str(payload.get("scientific_label", "")),
    )


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _git_state(root: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    return commit, dirty


def _environment(config: RealDataPilotConfig, commit: str, dirty: bool) -> dict[str, Any]:
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
        "commit_hash": commit,
        "git_dirty": dirty,
        "packages": packages,
        "duckdb": {
            "threads": config.duckdb_threads,
            "memory_limit_mb": config.duckdb_memory_limit_mb,
            "gpu_acceleration": False,
        },
    }


def _semantic_digest(columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...]) -> str:
    """Hash unordered relational results without retaining sensitive values."""

    def jsonable(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    normalized = [
        json.dumps(
            [jsonable(value) for value in row],
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
    ]
    payload = json.dumps(
        {"columns": columns, "rows": sorted(normalized)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _stage_statistics(connection: Any, workload: str) -> dict[str, int | float]:
    """Collect explanatory cardinalities outside the measured loop."""

    if workload == "bts":
        row = connection.execute(
            """
            SELECT
              COUNT(*) AS input_rows,
              COUNT(*) FILTER (WHERE FlightDate >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00'
                AND FlightDate < TIMESTAMPTZ '2024-01-22 00:00:00+00:00') AS temporal_rows,
              COUNT(*) FILTER (WHERE FlightDate >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00'
                AND FlightDate < TIMESTAMPTZ '2024-01-22 00:00:00+00:00'
                AND Distance >= 750.0 AND Cancelled = false) AS governed_rows
            FROM trust_bts_flights
            """
        ).fetchone()
        if row is None:
            raise GovernedRealDataSmokeError("BTS statistics query returned no row")
        input_rows, temporal_rows, governed_rows = (int(value) for value in row)
        return {
            "input_rows": input_rows,
            "temporal_rows": temporal_rows,
            "governed_rows": governed_rows,
            "temporal_selectivity": temporal_rows / input_rows if input_rows else 0.0,
            "governed_selectivity": governed_rows / input_rows if input_rows else 0.0,
        }

    row = connection.execute(
        """
        WITH filtered AS (
          SELECT * FROM trust_nyc_trips
          WHERE tpep_pickup_datetime >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00'
            AND tpep_pickup_datetime < TIMESTAMPTZ '2024-01-22 00:00:00+00:00'
            AND trip_distance >= 2.0 AND total_amount >= 10.0
        )
        SELECT
          (SELECT COUNT(*) FROM trust_nyc_trips) AS input_rows,
          (SELECT COUNT(*) FROM trust_nyc_trips
            WHERE tpep_pickup_datetime >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00'
              AND tpep_pickup_datetime < TIMESTAMPTZ '2024-01-22 00:00:00+00:00')
            AS temporal_rows,
          (SELECT COUNT(*) FROM filtered) AS governed_rows,
          (SELECT COUNT(*) FROM filtered f JOIN trust_nyc_zones z
            ON f.PULocationID = z.LocationID) AS joined_rows
        """
    ).fetchone()
    if row is None:
        raise GovernedRealDataSmokeError("NYC statistics query returned no row")
    input_rows, temporal_rows, governed_rows, joined_rows = (int(value) for value in row)
    return {
        "input_rows": input_rows,
        "temporal_rows": temporal_rows,
        "governed_rows": governed_rows,
        "joined_rows": joined_rows,
        "temporal_selectivity": temporal_rows / input_rows if input_rows else 0.0,
        "governed_selectivity": governed_rows / input_rows if input_rows else 0.0,
        "join_match_rate": joined_rows / governed_rows if governed_rows else 0.0,
    }


class _Progress:
    def __init__(self, total: int, enabled: bool) -> None:
        self.total = total
        self.enabled = enabled
        self.completed = 0
        self.started = time.monotonic()

    def advance(self, label: str) -> None:
        self.completed += 1
        if not self.enabled:
            return
        elapsed = time.monotonic() - self.started
        rate = self.completed / elapsed if elapsed > 0 else 0.0
        remaining = (self.total - self.completed) / rate if rate > 0 else 0.0
        width = 24
        filled = round(width * self.completed / self.total)
        bar = "#" * filled + "-" * (width - filled)
        print(
            f"\r[{bar}] {self.completed}/{self.total} "
            f"({100 * self.completed / self.total:5.1f}%) "
            f"elapsed={elapsed:6.1f}s ETA={remaining:6.1f}s {label:28.28s}",
            end="\n" if self.completed == self.total else "",
            flush=True,
        )


def _workload_plan(examples: Path, workload: str) -> dict[str, Any]:
    filename = "bts_governed_read.json" if workload == "bts" else "nyc_governed_aggregate.json"
    return _load_json(examples / "plans" / filename)


def _run_unit(
    *,
    root: Path,
    config: RealDataPilotConfig,
    workload: str,
    sample_rows: int,
    progress: _Progress,
) -> dict[str, Any]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for this pilot") from exc

    examples = root / "examples/real_data"
    artifact_bindings = verify_real_data_slice_artifacts(root / "data", sample_rows)
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    raw_plan = _workload_plan(examples, workload)
    unit_id = f"{workload}-n{sample_rows}"
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb" / unit_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        escaped_temp = str(temp_dir).replace("'", "''")
        connection.execute(f"SET temp_directory = '{escaped_temp}'")
        bindings = _create_trusted_views(
            connection,
            root / "data",
            sample_rows=sample_rows,
        )

        validation_started = time.perf_counter_ns()
        response = validate(raw_plan, policy, catalog)
        validation_latency_ms = (time.perf_counter_ns() - validation_started) / 1_000_000
        if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
            raise GovernedRealDataSmokeError(f"{unit_id} was not approved: {response.status}")
        plan: ValidatedLogicalPlan | None = response.validated_plan
        if plan is None:
            raise GovernedRealDataSmokeError(f"{unit_id} returned no validated plan")
        compile_started = time.perf_counter_ns()
        compiled = compile_validated_plan(plan, catalog, bindings)
        compile_latency_ms = (time.perf_counter_ns() - compile_started) / 1_000_000

        # One excluded preflight proves Mask/lineage/certificate semantics for
        # the exact sample binding before any latency sample is accepted.
        governed = _execute_governed_case(
            case_id=f"PILOT-{workload.upper()}-{sample_rows}",
            raw_plan=raw_plan,
            policy=policy,
            catalog=catalog,
            bindings=bindings,
            connection=connection,
            expect_masked_tail=workload == "bts",
        )
        progress.advance(f"{unit_id} semantic preflight")

        observation = observe_duckdb_plan(
            connection,
            compiled.sql,
            compiled.parameters,
            analyze=True,
        )
        progress.advance(f"{unit_id} physical profile")
        statistics_payload = _stage_statistics(connection, workload)

        expected_digest: str | None = None
        for warmup_index in range(config.warmup_runs):
            execution = execute_with_connection(compiled, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError(f"{unit_id} warmup result changed")
            progress.advance(f"{unit_id} warmup {warmup_index + 1}")

        measurements: list[RealDataPilotMeasurement] = []
        for repeat_index in range(config.measured_runs):
            started = time.perf_counter_ns()
            execution = execute_with_connection(compiled, connection)
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            if digest != expected_digest:
                raise GovernedRealDataSmokeError(f"{unit_id} measured result changed")
            measurements.append(
                RealDataPilotMeasurement(
                    unit_id=unit_id,
                    workload=workload,
                    sample_rows=sample_rows,
                    repeat_index=repeat_index,
                    client_materialization_latency_ms=latency_ms,
                    output_row_count=execution.row_count,
                    semantic_result_digest=digest,
                )
            )
            progress.advance(f"{unit_id} measured {repeat_index + 1}")
    finally:
        connection.close()

    latencies = [item.client_materialization_latency_ms for item in measurements]
    return {
        "unit_id": unit_id,
        "workload": workload,
        "sample_rows": sample_rows,
        "status": "PASS",
        "scientific_label": config.scientific_label,
        "candidate_count": 1,
        "optimizer_comparison_permitted": False,
        "verified_execution_artifacts": [asdict(item) for item in artifact_bindings],
        "validation_status": governed.validation_status,
        "validation_reason_codes": list(governed.reason_codes),
        "validation_latency_ms_single_observation": validation_latency_ms,
        "compile_latency_ms_single_observation": compile_latency_ms,
        "certificate_status": governed.certificate_status,
        "verified_obligations": list(governed.verified_obligations),
        "raw_sensitive_exposure_rows": governed.raw_sensitive_exposure_rows,
        "lineage_source_count": governed.lineage_source_count,
        "stage_statistics": statistics_payload,
        "physical_plan": {
            "fingerprint": observation.fingerprint,
            "operator_names": list(observation.operator_names),
            "actual_cardinalities": list(observation.actual_cardinalities),
            "rows_scanned": list(observation.rows_scanned),
            "profile_latency_ms_single_observation": observation.profile_latency_ms,
            "max_intermediate_cardinality": observation.max_intermediate_cardinality,
            "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
            "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
        },
        "latency_summary": {
            "scope": "query_execution_fetch_and_result_digest",
            "warmup_runs": config.warmup_runs,
            "measured_runs": config.measured_runs,
            "median_ms": statistics.median(latencies),
            "p95_ms": _percentile(latencies, 0.95),
            "min_ms": min(latencies),
            "max_ms": max(latencies),
        },
        "measurements": [asdict(item) for item in measurements],
    }


def _write_measurements(run_dir: Path, units: list[dict[str, Any]]) -> None:
    rows = [row for unit in units for row in unit["measurements"]]
    path = run_dir / "measurements.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(RealDataPilotMeasurement.__annotations__)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_real_data_pilot(
    config: RealDataPilotConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or resume atomic workload/size units and return the run directory."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("paper-like run requires a clean Git worktree")
    results_root = root / config.results_dir
    run_id = resume_run_id or _new_run_id()
    run_dir = results_root / run_id
    units_dir = run_dir / "units"
    units_dir.mkdir(parents=True, exist_ok=True)
    # Normalize tuples to their JSON list representation before both writing
    # and resume comparison, otherwise an unchanged config looks different.
    config_payload = json.loads(json.dumps(asdict(config), sort_keys=True))
    config_path = run_dir / "config.json"
    if resume_run_id is not None and config_path.is_file():
        frozen_config = json.loads(config_path.read_text(encoding="utf-8"))
        if frozen_config != config_payload:
            raise GovernedRealDataSmokeError(
                "resume config differs from the frozen run configuration"
            )
    _atomic_json(config_path, config_payload)
    _atomic_json(run_dir / "environment.json", _environment(config, commit, dirty))
    if resume_run_id is None:
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})

    schedule = [
        (workload, sample_rows)
        for workload in config.workloads
        for sample_rows in config.sample_rows
    ]
    random.Random(config.unit_order_seed).shuffle(schedule)
    completed = {path.stem for path in units_dir.glob("*.json") if path.is_file()}
    pending = [item for item in schedule if f"{item[0]}-n{item[1]}" not in completed]
    steps_per_unit = 2 + config.warmup_runs + config.measured_runs
    progress = _Progress(len(pending) * steps_per_unit, show_progress)

    for workload, sample_rows in pending:
        unit = _run_unit(
            root=root,
            config=config,
            workload=workload,
            sample_rows=sample_rows,
            progress=progress,
        )
        _atomic_json(units_dir / f"{unit['unit_id']}.json", unit)
        completed.add(str(unit["unit_id"]))
        progress_payload = {
            "run_id": run_id,
            "completed_units": len(completed),
            "total_units": len(schedule),
            "last_completed_unit": unit["unit_id"],
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }
        _atomic_json(run_dir / "progress.json", progress_payload)
        _atomic_json(results_root / "latest_progress.json", progress_payload)

    units = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(units_dir.glob("*.json"))
    ]
    _write_measurements(run_dir, units)
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "PASS",
        "scientific_label": config.scientific_label,
        "paper_performance_evidence": False,
        "optimizer_comparison_permitted": False,
        "candidate_count_per_unit": 1,
        "completed_units": len(units),
        "expected_units": len(schedule),
        "units": [
            {key: value for key, value in unit.items() if key != "measurements"} for unit in units
        ],
    }
    _atomic_json(run_dir / "summary.json", summary)
    return run_dir
