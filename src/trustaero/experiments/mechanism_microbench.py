"""Reproducible DuckDB microbenchmarks for governed Mask cost mechanisms.

The runner isolates three mechanisms used by early/late Mask plans: SHA-256
work, payload consumption across a Join, and explicit materialization write and
read.  Each configuration unit is checkpointed atomically, so an interrupted
unit is rerun in full rather than contributing partial timing samples.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
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

from trustaero.execution import observe_duckdb_plan

SUPPORTED_MICROBENCHMARKS = ("hash", "join_payload", "materialization")


@dataclass(frozen=True)
class MechanismMicrobenchConfig:
    """Frozen protocol for controlled mechanism-cost measurements."""

    results_dir: str
    row_counts: tuple[int, ...]
    identifier_widths: tuple[int, ...]
    match_rates: tuple[float, ...]
    seeds: tuple[int, ...]
    benchmarks: tuple[str, ...] = SUPPORTED_MICROBENCHMARKS
    warmup_runs: int = 3
    measured_runs: int = 15
    profile_runs: int = 3
    duckdb_threads: int = 4
    duckdb_memory_limit_mb: int = 4096
    order_seed: int = 20260717
    require_clean_git: bool = True

    def __post_init__(self) -> None:
        if not self.results_dir:
            raise ValueError("results_dir cannot be empty")
        if not self.row_counts or any(value < 1 for value in self.row_counts):
            raise ValueError("row_counts must contain positive values")
        if not self.identifier_widths or any(
            value < 1 or value > 4096 for value in self.identifier_widths
        ):
            raise ValueError("identifier_widths must be in [1, 4096]")
        if not self.match_rates or any(
            not 0.0 <= value <= 1.0 for value in self.match_rates
        ):
            raise ValueError("match_rates must be in [0, 1]")
        if not self.seeds or any(value < 0 for value in self.seeds):
            raise ValueError("seeds must contain non-negative values")
        for values, name in (
            (self.row_counts, "row_counts"),
            (self.identifier_widths, "identifier_widths"),
            (self.match_rates, "match_rates"),
            (self.seeds, "seeds"),
            (self.benchmarks, "benchmarks"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} cannot contain duplicates")
        if not self.benchmarks or any(
            value not in SUPPORTED_MICROBENCHMARKS for value in self.benchmarks
        ):
            raise ValueError("benchmarks contain an unsupported mechanism")
        if self.warmup_runs < 0 or self.measured_runs < 1 or self.profile_runs < 1:
            raise ValueError(
                "warmup_runs must be non-negative; measured_runs and profile_runs positive"
            )
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("DuckDB resource limits are invalid")


@dataclass(frozen=True)
class MechanismMicrobenchUnit:
    """One atomic mechanism, scale, width, match-rate, and seed combination."""

    benchmark: str
    row_count: int
    identifier_width: int
    match_rate: float | None
    seed: int

    @property
    def unit_id(self) -> str:
        match = (
            "na" if self.match_rate is None else f"{round(self.match_rate * 1000):04d}"
        )
        return (
            f"{self.benchmark}-n{self.row_count}-w{self.identifier_width}"
            f"-m{match}-s{self.seed}"
        )


def mechanism_microbench_units(
    config: MechanismMicrobenchConfig,
) -> tuple[MechanismMicrobenchUnit, ...]:
    """Expand only dimensions that affect a mechanism.

    Hash and materialization have no match-rate axis. Join payload explicitly
    crosses the declared match rates while holding the dimension side fixed.
    """

    output: list[MechanismMicrobenchUnit] = []
    for benchmark in config.benchmarks:
        rates: tuple[float | None, ...] = (
            tuple(config.match_rates) if benchmark == "join_payload" else (None,)
        )
        for row_count in config.row_counts:
            for width in config.identifier_widths:
                for match_rate in rates:
                    for seed in config.seeds:
                        output.append(
                            MechanismMicrobenchUnit(
                                benchmark=benchmark,
                                row_count=row_count,
                                identifier_width=width,
                                match_rate=match_rate,
                                seed=seed,
                            )
                        )
    return tuple(output)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


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


def _git_dirty(root: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "status", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(completed.stdout.strip())


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, default=str, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    # Virus scanners and concurrent readers can briefly retain a Windows file
    # handle after reading the progress file. Retry only that transient atomic
    # replace; all other filesystem errors still fail immediately.
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (2**attempt))


def _environment(
    commit_hash: str,
    git_dirty: bool,
    config: MechanismMicrobenchConfig,
) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for package in ("duckdb", "trustaero"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "not-installed"
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "commit_hash": commit_hash,
        "git_dirty": git_dirty,
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "packages": packages,
        "duckdb_threads": config.duckdb_threads,
        "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
    }


def _create_data(connection: Any, unit: MechanismMicrobenchUnit) -> int:
    """Create deterministic exact-width strings and a fixed-size Join build side."""

    connection.execute("DROP TABLE IF EXISTS micro_materialized")
    connection.execute("DROP TABLE IF EXISTS micro_join_output")
    connection.execute("DROP TABLE IF EXISTS micro_events")
    connection.execute("DROP TABLE IF EXISTS micro_dimension")
    blocks = math.ceil(unit.identifier_width / 32)
    matched_rows = (
        unit.row_count
        if unit.match_rate is None
        else round(unit.row_count * unit.match_rate)
    )
    dimension_rows = min(unit.row_count, 10_000)
    connection.execute(
        f"""
        CREATE TABLE micro_events AS
        SELECT
            i::BIGINT AS row_id,
            left(
                repeat(md5(CAST(i + {unit.seed * 1_000_003} AS VARCHAR)), {blocks}),
                {unit.identifier_width}
            ) AS sensitive_value,
            CASE
                WHEN i < {matched_rows} THEN (i % {dimension_rows})::BIGINT
                ELSE ({dimension_rows} + i)::BIGINT
            END AS join_key,
            i < {matched_rows} AS will_match
        FROM range({unit.row_count}) AS source(i)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE micro_dimension AS
        SELECT i::BIGINT AS dimension_key, (i % 97)::BIGINT AS marker
        FROM range({dimension_rows}) AS source(i)
        """
    )
    observed = connection.execute(
        "SELECT count(*), min(length(sensitive_value)), max(length(sensitive_value)) "
        "FROM micro_events"
    ).fetchone()
    if observed != (unit.row_count, unit.identifier_width, unit.identifier_width):
        raise ValueError(f"Generated data failed width validation for {unit.unit_id}")
    return matched_rows


def _component_sql(benchmark: str) -> dict[str, str]:
    if benchmark == "hash":
        return {
            "hash_scan": "SELECT sum(length(sensitive_value))::HUGEINT FROM micro_events",
            "hash_sha256": (
                "SELECT sum(length(sha256(sensitive_value)))::HUGEINT FROM micro_events"
            ),
        }
    if benchmark == "join_payload":
        return {
            "join_payload_baseline": (
                "CREATE TEMP TABLE micro_join_output AS "
                "SELECT row_id, sensitive_value, 0::BIGINT AS marker "
                "FROM micro_events WHERE will_match"
            ),
            "join_payload": (
                "CREATE TEMP TABLE micro_join_output AS "
                "SELECT events.row_id, events.sensitive_value, dimension.marker "
                "FROM micro_events AS events INNER JOIN micro_dimension AS dimension "
                "ON events.join_key = dimension.dimension_key"
            ),
        }
    if benchmark == "materialization":
        return {
            "materialization_write": (
                "CREATE TEMP TABLE micro_materialized AS "
                "SELECT row_id, sensitive_value FROM micro_events"
            ),
            "materialization_read": (
                "SELECT count(*)::BIGINT, sum(length(sensitive_value))::HUGEINT "
                "FROM micro_materialized"
            ),
        }
    raise ValueError(f"Unsupported microbenchmark: {benchmark}")


def _validate_result(
    unit: MechanismMicrobenchUnit,
    component: str,
    result: tuple[Any, ...],
    matched_rows: int,
) -> None:
    if component == "hash_scan" and result != (unit.row_count * unit.identifier_width,):
        raise ValueError("Hash scan checksum is inconsistent")
    if component == "hash_sha256" and result != (unit.row_count * 64,):
        raise ValueError("SHA-256 output checksum is inconsistent")
    if component in {"join_payload_baseline", "join_payload"}:
        if result != (matched_rows, matched_rows * unit.identifier_width):
            raise ValueError("Join match cardinality is inconsistent")
    if component in {"materialization_write", "materialization_read"}:
        if result != (unit.row_count, unit.row_count * unit.identifier_width):
            raise ValueError("Materialized payload checksum is inconsistent")


def _execute_component(
    connection: Any,
    unit: MechanismMicrobenchUnit,
    component: str,
    sql: str,
    matched_rows: int,
) -> tuple[float, tuple[Any, ...]]:
    output_table: str | None = None
    if component == "materialization_write":
        output_table = "micro_materialized"
    elif component in {"join_payload_baseline", "join_payload"}:
        output_table = "micro_join_output"
    if output_table is not None:
        connection.execute(f"DROP TABLE IF EXISTS {output_table}")
        started = time.perf_counter()
        connection.execute(sql)
        latency_ms = (time.perf_counter() - started) * 1000.0
        result = connection.execute(
            "SELECT count(*)::BIGINT, sum(length(sensitive_value))::HUGEINT "
            f"FROM {output_table}"
        ).fetchone()
    else:
        started = time.perf_counter()
        result = connection.execute(sql).fetchone()
        latency_ms = (time.perf_counter() - started) * 1000.0
    if result is None:
        raise ValueError(f"Microbenchmark component returned no result: {component}")
    output = tuple(result)
    _validate_result(unit, component, output, matched_rows)
    return latency_ms, output


def _component_orders(
    components: tuple[str, ...],
    round_count: int,
    offset: int,
) -> tuple[tuple[str, ...], ...]:
    if components == ("materialization_write", "materialization_read"):
        return tuple(components for _ in range(round_count))
    values = list(components)
    return tuple(
        tuple(
            values[(offset + index) % len(values) :]
            + values[: (offset + index) % len(values)]
        )
        for index in range(round_count)
    )


def _profiles(
    connection: Any,
    unit: MechanismMicrobenchUnit,
    sql_by_component: dict[str, str],
    output_dir: Path,
    profile_runs: int,
) -> dict[str, dict[str, Any]]:
    """Capture real operator trees once; timed samples remain plain executions."""

    output: dict[str, dict[str, Any]] = {}
    profile_dir = output_dir / "plans" / unit.unit_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_components: tuple[str, ...]
    if unit.benchmark == "materialization":
        connection.execute("DROP TABLE IF EXISTS micro_materialized")
        profile_components = ("materialization_write", "materialization_read")
    elif unit.benchmark == "join_payload":
        profile_components = tuple(sql_by_component)
    else:
        profile_components = tuple(sql_by_component)
    for component in profile_components:
        observations = []
        for profile_index in range(profile_runs):
            if component == "materialization_write":
                connection.execute("DROP TABLE IF EXISTS micro_materialized")
            elif component == "materialization_read":
                if connection.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'micro_materialized'"
                ).fetchone() == (0,):
                    connection.execute(sql_by_component["materialization_write"])
            elif component in {"join_payload_baseline", "join_payload"}:
                connection.execute("DROP TABLE IF EXISTS micro_join_output")
            observation = observe_duckdb_plan(
                connection,
                sql_by_component[component],
                analyze=True,
            )
            observations.append(observation)
            (profile_dir / f"{component}-analyze-r{profile_index}.json").write_text(
                observation.plan_json + "\n",
                encoding="utf-8",
            )
            if component in {"join_payload_baseline", "join_payload"}:
                connection.execute("DROP TABLE IF EXISTS micro_join_output")
        if len({item.operator_names for item in observations}) != 1:
            raise ValueError(
                f"Physical operator shape changed within {component} profiles"
            )
        if len({item.fingerprint for item in observations}) != 1:
            raise ValueError(
                f"Physical plan fingerprint changed within {component} profiles"
            )
        reference = observations[0]
        output[component] = {
            "fingerprint": reference.fingerprint,
            "profile_runs": profile_runs,
            "profile_latency_ms": statistics.median(
                item.profile_latency_ms for item in observations
            ),
            "operator_names": list(reference.operator_names),
            "operator_timings_ms": [
                statistics.median(
                    item.operator_timings_ms[index] for item in observations
                )
                for index in range(len(reference.operator_names))
            ],
            "operator_cardinalities": list(reference.actual_cardinalities),
            "rows_scanned": list(reference.rows_scanned),
            "peak_buffer_memory_bytes": max(
                item.peak_buffer_memory_bytes for item in observations
            ),
            "peak_temp_directory_bytes": max(
                item.peak_temp_directory_bytes for item in observations
            ),
            "total_memory_allocated_bytes": max(
                item.total_memory_allocated_bytes for item in observations
            ),
            "profile_samples": [
                {
                    "profile_latency_ms": item.profile_latency_ms,
                    "operator_timings_ms": list(item.operator_timings_ms),
                }
                for item in observations
            ],
        }
    if unit.benchmark == "materialization":
        connection.execute("DROP TABLE IF EXISTS micro_materialized")
    return output


def _run_unit(
    connection: Any,
    config: MechanismMicrobenchConfig,
    unit: MechanismMicrobenchUnit,
    *,
    run_id: str,
    commit_hash: str,
    output_dir: Path,
) -> dict[str, Any]:
    matched_rows = _create_data(connection, unit)
    sql_by_component = _component_sql(unit.benchmark)
    profiles = _profiles(
        connection,
        unit,
        sql_by_component,
        output_dir,
        config.profile_runs,
    )
    components = tuple(sql_by_component)
    total_rounds = config.warmup_runs + config.measured_runs
    offset = int.from_bytes(
        hashlib.sha256(f"{unit.unit_id}:{config.order_seed}".encode()).digest()[:4],
        "big",
    )
    orders = _component_orders(components, total_rounds, offset)
    measurements: list[dict[str, Any]] = []
    for round_index, order in enumerate(orders):
        is_warmup = round_index < config.warmup_runs
        repeat_index = round_index - config.warmup_runs
        for position, component in enumerate(order):
            latency_ms, result = _execute_component(
                connection,
                unit,
                component,
                sql_by_component[component],
                matched_rows,
            )
            if not is_warmup:
                measurements.append(
                    {
                        "run_id": run_id,
                        "commit_hash": commit_hash,
                        "unit_id": unit.unit_id,
                        "benchmark": unit.benchmark,
                        "row_count": unit.row_count,
                        "identifier_width": unit.identifier_width,
                        "match_rate": unit.match_rate,
                        "matched_rows": matched_rows,
                        "seed": unit.seed,
                        "repeat_index": repeat_index,
                        "order_position": position,
                        "component": component,
                        "latency_ms": latency_ms,
                        "result_digest": _digest(result),
                        "logical_payload_bytes": (
                            matched_rows * unit.identifier_width
                            if unit.benchmark == "join_payload"
                            else unit.row_count * unit.identifier_width
                        ),
                    }
                )
    return {
        "unit": asdict(unit),
        "unit_id": unit.unit_id,
        "matched_rows": matched_rows,
        "profiles": profiles,
        "measurements": measurements,
        "validation_passed": True,
    }


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _finalize(
    output_dir: Path,
    config: MechanismMicrobenchConfig,
    run_id: str,
) -> None:
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((output_dir / "units").glob("*.json"))
    ]
    measurements = [row for payload in payloads for row in payload["measurements"]]
    component_rows: list[dict[str, Any]] = []
    paired_rows: list[dict[str, Any]] = []
    operator_rows: list[dict[str, Any]] = []
    for payload in payloads:
        unit = payload["unit"]
        by_component: dict[str, list[dict[str, Any]]] = {}
        for row in payload["measurements"]:
            by_component.setdefault(str(row["component"]), []).append(row)
        for component, rows in sorted(by_component.items()):
            latencies = [float(row["latency_ms"]) for row in rows]
            payload_bytes = int(rows[0]["logical_payload_bytes"])
            median_ms = statistics.median(latencies)
            profile = payload["profiles"].get(component, {})
            component_rows.append(
                {
                    "unit_id": payload["unit_id"],
                    **unit,
                    "matched_rows": payload["matched_rows"],
                    "component": component,
                    "runs": len(rows),
                    "median_latency_ms": median_ms,
                    "p95_latency_ms": _percentile95(latencies),
                    "min_latency_ms": min(latencies),
                    "max_latency_ms": max(latencies),
                    "logical_payload_bytes": payload_bytes,
                    "median_mib_per_second": (
                        payload_bytes / (1024 * 1024) / (median_ms / 1000.0)
                        if median_ms > 0.0
                        else None
                    ),
                    "physical_plan_fingerprint": profile.get("fingerprint"),
                    "physical_operator_names": "|".join(
                        profile.get("operator_names", [])
                    ),
                    "peak_buffer_memory_bytes": profile.get("peak_buffer_memory_bytes"),
                    "peak_temp_directory_bytes": profile.get(
                        "peak_temp_directory_bytes"
                    ),
                }
            )
        for component, profile in payload["profiles"].items():
            samples = profile["profile_samples"]
            for operator_index, operator_name in enumerate(profile["operator_names"]):
                timings = [
                    float(sample["operator_timings_ms"][operator_index])
                    for sample in samples
                ]
                operator_rows.append(
                    {
                        "unit_id": payload["unit_id"],
                        **unit,
                        "matched_rows": payload["matched_rows"],
                        "component": component,
                        "operator_index": operator_index,
                        "operator_name": operator_name,
                        "profile_runs": len(timings),
                        "median_operator_timing_ms": statistics.median(timings),
                        "p95_operator_timing_ms": _percentile95(timings),
                        "min_operator_timing_ms": min(timings),
                        "max_operator_timing_ms": max(timings),
                        "actual_cardinality": profile["operator_cardinalities"][
                            operator_index
                        ],
                        "rows_scanned": profile["rows_scanned"][operator_index],
                        "physical_plan_fingerprint": profile["fingerprint"],
                    }
                )
        paired_components = {
            "hash": ("hash_sha256", "hash_scan", "hash_incremental", "subtract"),
            "join_payload": (
                "join_payload",
                "join_payload_baseline",
                "join_incremental",
                "subtract",
            ),
            "materialization": (
                "materialization_write",
                "materialization_read",
                "materialization_roundtrip",
                "sum",
            ),
        }
        left_name, right_name, derived_name, operation = paired_components[
            str(unit["benchmark"])
        ]
        left = {
            int(row["repeat_index"]): float(row["latency_ms"])
            for row in by_component[left_name]
        }
        right = {
            int(row["repeat_index"]): float(row["latency_ms"])
            for row in by_component[right_name]
        }
        paired_values = [
            (
                left[index] + right[index]
                if operation == "sum"
                else left[index] - right[index]
            )
            for index in sorted(set(left) & set(right))
        ]
        paired_rows.append(
            {
                "unit_id": payload["unit_id"],
                **unit,
                "matched_rows": payload["matched_rows"],
                "derived_component": derived_name,
                "left_component": left_name,
                "right_component": right_name,
                "paired_operation": operation,
                "pairs": len(paired_values),
                "median_paired_cost_ms": statistics.median(paired_values),
                "p95_paired_cost_ms": _percentile95(paired_values),
                "min_paired_cost_ms": min(paired_values),
                "max_paired_cost_ms": max(paired_values),
            }
        )
    _write_csv(output_dir / "raw_measurements.csv", measurements)
    _write_csv(output_dir / "component_summary.csv", component_rows)
    _write_csv(output_dir / "paired_costs.csv", paired_rows)
    _write_csv(output_dir / "operator_summary.csv", operator_rows)
    negative = sum(float(row["median_paired_cost_ms"]) < 0.0 for row in paired_rows)
    _write_json_atomic(
        output_dir / "summary.json",
        {
            "run_id": run_id,
            "status": "complete",
            "evaluation_label": "mechanism_microbenchmark",
            "unit_count": len(payloads),
            "measurement_count": len(measurements),
            "component_summary_count": len(component_rows),
            "paired_cost_count": len(paired_rows),
            "operator_summary_count": len(operator_rows),
            "all_validations_passed": all(
                payload.get("validation_passed") is True for payload in payloads
            ),
            "negative_median_paired_difference_count": negative,
            "benchmarks": list(config.benchmarks),
            "note": (
                "Paired differences are diagnostics, not guaranteed pure operator costs; "
                "DuckDB may fuse or defer payload work. Physical profiles are retained."
            ),
        },
    )


def run_mechanism_microbench(
    config: MechanismMicrobenchConfig,
    *,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or safely resume the controlled DuckDB mechanism microbenchmarks."""

    import duckdb

    root = _repo_root()
    commit_hash = _git_commit(root)
    git_dirty = _git_dirty(root)
    if config.require_clean_git and git_dirty:
        raise ValueError("This mechanism microbenchmark requires a clean Git worktree")
    results_root = root / config.results_dir
    results_root.mkdir(parents=True, exist_ok=True)
    run_id = resume_run_id or _new_run_id()
    output_dir = results_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    config_payload = asdict(config)
    config_digest = _digest(config_payload)
    checkpoint_path = output_dir / "checkpoint.json"
    if resume_run_id:
        if not checkpoint_path.exists():
            raise ValueError(f"Cannot resume missing mechanism run: {resume_run_id}")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != config_digest:
            raise ValueError("Resume config does not match the original mechanism run")
        environment = json.loads(
            (output_dir / "environment.json").read_text(encoding="utf-8")
        )
        if environment.get("commit_hash") != commit_hash:
            raise ValueError("Cannot resume mechanism run after the Git commit changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_units": [],
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write_json_atomic(output_dir / "config.json", config_payload)
        _write_json_atomic(
            output_dir / "environment.json",
            _environment(commit_hash, git_dirty, config),
        )
        _write_json_atomic(checkpoint_path, checkpoint)
        _write_json_atomic(results_root / "latest_run.json", {"run_id": run_id})

    units = mechanism_microbench_units(config)
    completed = set(checkpoint["completed_units"])
    started = time.perf_counter()
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        for unit in units:
            if unit.unit_id in completed:
                continue
            try:
                payload = _run_unit(
                    connection,
                    config,
                    unit,
                    run_id=run_id,
                    commit_hash=commit_hash,
                    output_dir=output_dir,
                )
            except Exception as error:
                _write_json_atomic(
                    output_dir / "failures" / f"{unit.unit_id}.json",
                    {
                        "unit_id": unit.unit_id,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
                raise
            _write_json_atomic(output_dir / "units" / f"{unit.unit_id}.json", payload)
            completed.add(unit.unit_id)
            checkpoint["completed_units"] = sorted(completed)
            checkpoint["updated_at"] = datetime.now(UTC).isoformat()
            _write_json_atomic(checkpoint_path, checkpoint)
            elapsed = time.perf_counter() - started
            done = len(completed)
            eta = elapsed / done * (len(units) - done) if done else 0.0
            progress = {
                "run_id": run_id,
                "completed_units": done,
                "total_units": len(units),
                "fraction": done / len(units),
                "current_unit": unit.unit_id,
                "elapsed_seconds": elapsed,
                "eta_seconds": eta,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            _write_json_atomic(output_dir / "progress.json", progress)
            _write_json_atomic(results_root / "latest_progress.json", progress)
            if show_progress:
                print(
                    f"[microbench {done}/{len(units)}] {unit.unit_id} "
                    f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
                    flush=True,
                )
    finally:
        connection.close()

    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    _write_json_atomic(checkpoint_path, checkpoint)
    _finalize(output_dir, config, run_id)
    return output_dir


def load_mechanism_microbench_config(path: str | Path) -> MechanismMicrobenchConfig:
    """Load a versioned JSON mechanism protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return MechanismMicrobenchConfig(
        results_dir=str(payload["results_dir"]),
        row_counts=tuple(int(value) for value in payload["row_counts"]),
        identifier_widths=tuple(int(value) for value in payload["identifier_widths"]),
        match_rates=tuple(float(value) for value in payload["match_rates"]),
        seeds=tuple(int(value) for value in payload["seeds"]),
        benchmarks=tuple(
            str(value) for value in payload.get("benchmarks", SUPPORTED_MICROBENCHMARKS)
        ),
        warmup_runs=int(payload.get("warmup_runs", 3)),
        measured_runs=int(payload.get("measured_runs", 15)),
        profile_runs=int(payload.get("profile_runs", 3)),
        duckdb_threads=int(payload.get("duckdb_threads", 4)),
        duckdb_memory_limit_mb=int(payload.get("duckdb_memory_limit_mb", 4096)),
        order_seed=int(payload.get("order_seed", 20260717)),
        require_clean_git=bool(payload.get("require_clean_git", True)),
    )
