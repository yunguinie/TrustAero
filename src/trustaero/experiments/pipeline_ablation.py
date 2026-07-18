"""Checkpointed complete-pipeline ablation for governed Mask placement.

The smoke protocol compares four result-equivalent SQL fragments that place
materialization boundaries around Join, SHA-256, and ordered output.  It first
validates the actual DuckDB plan tree; timings are diagnostics only when every
semantic, plan-shape, cardinality, and spill check passes.
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
from typing import Any, cast

from trustaero.execution import observe_duckdb_plan

PIPELINE_ABLATION_VARIANTS = (
    "late_fused",
    "late_join_materialized",
    "late_hash_materialized",
    "early_hash_materialized",
)


@dataclass(frozen=True)
class PipelineAblationScenario:
    """One preselected development region and deterministic data seed."""

    scenario_id: str
    region_label: str
    row_count: int
    identifier_width: int
    match_rate: float
    seed: int

    def __post_init__(self) -> None:
        if not self.scenario_id or not self.region_label:
            raise ValueError("Ablation scenario labels cannot be empty")
        if self.row_count <= 0 or not 1 <= self.identifier_width <= 4096:
            raise ValueError("Ablation scenario scale or width is invalid")
        if not 0.0 <= self.match_rate <= 1.0 or self.seed < 0:
            raise ValueError("Ablation match rate or seed is invalid")


@dataclass(frozen=True)
class PipelineAblationConfig:
    """Frozen smoke protocol and DuckDB resource limits."""

    results_dir: str
    scenarios: tuple[PipelineAblationScenario, ...]
    warmup_runs: int = 1
    measured_runs: int = 3
    profile_runs: int = 1
    duckdb_threads: int = 4
    duckdb_memory_limit_mb: int = 4096
    order_seed: int = 20260718
    require_clean_git: bool = True

    def __post_init__(self) -> None:
        if not self.results_dir or not self.scenarios:
            raise ValueError("Ablation results directory and scenarios are required")
        ids = [item.scenario_id for item in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("Ablation scenario IDs cannot contain duplicates")
        if self.warmup_runs < 0 or self.measured_runs < 1 or self.profile_runs < 1:
            raise ValueError("Ablation repetition counts are invalid")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("Ablation DuckDB resource limits are invalid")


@dataclass(frozen=True)
class PipelineAblationExposure:
    """Governance-relevant intermediate exposure for one physical variant."""

    raw_rows_exposed_to_join: int
    raw_rows_materialized: int
    masked_rows_materialized: int

    def __post_init__(self) -> None:
        if min(
            self.raw_rows_exposed_to_join,
            self.raw_rows_materialized,
            self.masked_rows_materialized,
        ) < 0:
            raise ValueError("Ablation exposure row counts cannot be negative")


def pipeline_ablation_exposure(
    scenario: PipelineAblationScenario,
    variant: str,
) -> PipelineAblationExposure:
    """Return exposure annotations before any runtime ranking occurs.

    Raw Join exposure counts fact-side rows entering the Join, matching the
    conservative V1 convention. Raw materialization counts matched rows written
    by the Join-materialized diagnostic. Masked materialization is safe from
    raw-value exposure but still carries a physical cost.
    """

    if variant not in PIPELINE_ABLATION_VARIANTS:
        raise ValueError(f"Unknown ablation variant: {variant}")
    matched_rows = round(scenario.row_count * scenario.match_rate)
    if variant == "late_fused":
        return PipelineAblationExposure(scenario.row_count, 0, 0)
    if variant == "late_join_materialized":
        return PipelineAblationExposure(
            scenario.row_count, matched_rows, 0
        )
    if variant == "late_hash_materialized":
        return PipelineAblationExposure(
            scenario.row_count, 0, matched_rows
        )
    return PipelineAblationExposure(0, 0, scenario.row_count)


def pipeline_ablation_sql() -> dict[str, str]:
    """Return the four frozen, result-equivalent physical-fragment requests."""

    prefix = "CREATE TEMP TABLE ablation_output AS "
    return {
        "late_fused": (
            prefix
            + "SELECT events.row_id, sha256(events.sensitive_value) AS masked_value, "
            "dimension.marker FROM ablation_events AS events "
            "INNER JOIN ablation_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key "
            "ORDER BY masked_value, events.row_id"
        ),
        "late_join_materialized": (
            prefix
            + "WITH joined_events AS MATERIALIZED ("
            "SELECT events.row_id, events.sensitive_value, dimension.marker "
            "FROM ablation_events AS events "
            "INNER JOIN ablation_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key"
            ") SELECT joined.row_id, sha256(joined.sensitive_value) AS masked_value, "
            "joined.marker FROM joined_events AS joined "
            "ORDER BY masked_value, joined.row_id"
        ),
        "late_hash_materialized": (
            prefix
            + "WITH masked_events AS MATERIALIZED ("
            "SELECT events.row_id, sha256(events.sensitive_value) AS masked_value, "
            "dimension.marker FROM ablation_events AS events "
            "INNER JOIN ablation_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key"
            ") SELECT masked.row_id, masked.masked_value, masked.marker "
            "FROM masked_events AS masked "
            "ORDER BY masked.masked_value, masked.row_id"
        ),
        "early_hash_materialized": (
            prefix
            + "WITH masked_events AS MATERIALIZED ("
            "SELECT row_id, sha256(sensitive_value) AS masked_value, join_key "
            "FROM ablation_events"
            ") SELECT masked.row_id, masked.masked_value, dimension.marker "
            "FROM masked_events AS masked "
            "INNER JOIN ablation_dimension AS dimension "
            "ON masked.join_key = dimension.dimension_key "
            "ORDER BY masked.masked_value, masked.row_id"
        ),
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (2**attempt))


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


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _environment(
    commit_hash: str, git_dirty: bool, config: PipelineAblationConfig
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


def _create_data(connection: Any, scenario: PipelineAblationScenario) -> int:
    """Create exact-width data with deterministic, exact Join cardinality."""

    for table in ("ablation_output", "ablation_events", "ablation_dimension"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    blocks = math.ceil(scenario.identifier_width / 32)
    matched_rows = round(scenario.row_count * scenario.match_rate)
    dimension_rows = min(scenario.row_count, 10_000)
    connection.execute(
        f"""
        CREATE TABLE ablation_events AS
        SELECT
            i::BIGINT AS row_id,
            left(
                repeat(md5(CAST(i + {scenario.seed * 1_000_003} AS VARCHAR)), {blocks}),
                {scenario.identifier_width}
            ) AS sensitive_value,
            CASE
                WHEN i < {matched_rows} THEN (i % {dimension_rows})::BIGINT
                ELSE ({dimension_rows} + i)::BIGINT
            END AS join_key
        FROM range({scenario.row_count}) AS source(i)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE ablation_dimension AS
        SELECT i::BIGINT AS dimension_key, (i % 97)::BIGINT AS marker
        FROM range({dimension_rows}) AS source(i)
        """
    )
    observed = connection.execute(
        "SELECT count(*), min(length(sensitive_value)), max(length(sensitive_value)) "
        "FROM ablation_events"
    ).fetchone()
    if observed != (
        scenario.row_count,
        scenario.identifier_width,
        scenario.identifier_width,
    ):
        raise ValueError(f"Generated data failed validation: {scenario.scenario_id}")
    return matched_rows


def _walk_plan(node: dict[str, Any]) -> list[dict[str, Any]]:
    output = [node]
    children = node.get("children", [])
    if not isinstance(children, list):
        raise ValueError("DuckDB plan children must be a list")
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("DuckDB plan child must be an object")
        output.extend(_walk_plan(cast(dict[str, Any], child)))
    return output


def _operator_names(node: dict[str, Any]) -> list[str]:
    return [str(item.get("operator_name", "")) for item in _walk_plan(node)]


def _contains_masked_projection(node: dict[str, Any]) -> bool:
    for item in _walk_plan(node):
        if item.get("operator_name") != "PROJECTION":
            continue
        extra = json.dumps(item.get("extra_info", {}), sort_keys=True)
        if "masked_value" in extra:
            return True
    return False


def validate_ablation_plan_boundary(variant: str, plan_json: str) -> dict[str, Any]:
    """Fail closed unless DuckDB preserved the requested physical boundary."""

    if variant not in PIPELINE_ABLATION_VARIANTS:
        raise ValueError(f"Unknown ablation variant: {variant}")
    payload = json.loads(plan_json)
    if not isinstance(payload, dict):
        raise ValueError("DuckDB plan must contain a JSON object")
    root = cast(dict[str, Any], payload)
    all_nodes = _walk_plan(root)
    names = _operator_names(root)
    if names.count("HASH_JOIN") != 1 or names.count("ORDER_BY") != 1:
        raise ValueError(f"{variant} lacks one physical Join or sort")
    if not _contains_masked_projection(root):
        raise ValueError(f"{variant} lacks a physical masked-value projection")
    cte_nodes = [item for item in all_nodes if item.get("operator_name") == "CTE"]
    if variant == "late_fused":
        if cte_nodes:
            raise ValueError("late_fused unexpectedly retained a CTE boundary")
        return {
            "cte_count": 0,
            "producer_contains_join": False,
            "producer_contains_hash_projection": False,
            "consumer_contains_join": True,
            "consumer_contains_hash_projection": True,
            "consumer_contains_sort": True,
        }
    if len(cte_nodes) != 1:
        raise ValueError(f"{variant} must retain exactly one CTE boundary")
    children = cte_nodes[0].get("children", [])
    if not isinstance(children, list) or len(children) != 2:
        raise ValueError(f"{variant} CTE must expose producer and consumer branches")
    producer = cast(dict[str, Any], children[0])
    consumer = cast(dict[str, Any], children[1])
    producer_names = _operator_names(producer)
    consumer_names = _operator_names(consumer)
    producer_join = "HASH_JOIN" in producer_names
    producer_hash = _contains_masked_projection(producer)
    consumer_join = "HASH_JOIN" in consumer_names
    consumer_hash = _contains_masked_projection(consumer)
    consumer_sort = "ORDER_BY" in consumer_names
    expected = {
        "late_join_materialized": (True, False, False, True, True),
        "late_hash_materialized": (True, True, False, False, True),
        "early_hash_materialized": (False, True, True, False, True),
    }[variant]
    observed = (
        producer_join,
        producer_hash,
        consumer_join,
        consumer_hash,
        consumer_sort,
    )
    if observed != expected:
        raise ValueError(
            f"{variant} physical boundary differs: expected {expected}, got {observed}"
        )
    return {
        "cte_count": 1,
        "producer_contains_join": producer_join,
        "producer_contains_hash_projection": producer_hash,
        "consumer_contains_join": consumer_join,
        "consumer_contains_hash_projection": consumer_hash,
        "consumer_contains_sort": consumer_sort,
    }


def _result_checksum(connection: Any) -> tuple[Any, ...]:
    result = connection.execute(
        "SELECT count(*)::BIGINT, sum(length(masked_value))::HUGEINT, "
        "sum(row_id)::HUGEINT, sum(marker)::HUGEINT, "
        "bit_xor(hash(row_id, masked_value, marker)) FROM ablation_output"
    ).fetchone()
    if result is None:
        raise ValueError("Ablation output checksum returned no row")
    return tuple(result)


def _execute_variant(
    connection: Any,
    scenario: PipelineAblationScenario,
    variant: str,
    sql: str,
    matched_rows: int,
) -> tuple[float, tuple[Any, ...]]:
    connection.execute("DROP TABLE IF EXISTS ablation_output")
    started = time.perf_counter()
    connection.execute(sql)
    latency_ms = (time.perf_counter() - started) * 1000.0
    result = _result_checksum(connection)
    if result[:2] != (matched_rows, matched_rows * 64):
        raise ValueError(
            f"{scenario.scenario_id}/{variant} cardinality or hash width is invalid"
        )
    return latency_ms, result


def _variant_orders(round_count: int, offset: int) -> tuple[tuple[str, ...], ...]:
    values = list(PIPELINE_ABLATION_VARIANTS)
    return tuple(
        tuple(
            values[(offset + index) % len(values) :]
            + values[: (offset + index) % len(values)]
        )
        for index in range(round_count)
    )


def _profile_variants(
    connection: Any,
    scenario: PipelineAblationScenario,
    sql_by_variant: dict[str, str],
    output_dir: Path,
    profile_runs: int,
) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    plan_dir = output_dir / "plans" / scenario.scenario_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    for variant, sql in sql_by_variant.items():
        observations = []
        boundaries = []
        for profile_index in range(profile_runs):
            connection.execute("DROP TABLE IF EXISTS ablation_output")
            observation = observe_duckdb_plan(connection, sql, analyze=True)
            boundary = validate_ablation_plan_boundary(variant, observation.plan_json)
            observations.append(observation)
            boundaries.append(boundary)
            (plan_dir / f"{variant}-analyze-r{profile_index}.json").write_text(
                observation.plan_json + "\n", encoding="utf-8"
            )
        if len({item.fingerprint for item in observations}) != 1:
            raise ValueError(f"{variant} physical fingerprint changed across profiles")
        if len({item.operator_names for item in observations}) != 1:
            raise ValueError(f"{variant} physical shape changed across profiles")
        if any(item != boundaries[0] for item in boundaries):
            raise ValueError(f"{variant} boundary validation changed across profiles")
        reference = observations[0]
        profiles[variant] = {
            "fingerprint": reference.fingerprint,
            "operator_names": list(reference.operator_names),
            "operator_timings_ms": [
                statistics.median(
                    item.operator_timings_ms[index] for item in observations
                )
                for index in range(len(reference.operator_names))
            ],
            "operator_cardinalities": list(reference.actual_cardinalities),
            "rows_scanned": list(reference.rows_scanned),
            "profile_runs": profile_runs,
            "profile_latency_ms": statistics.median(
                item.profile_latency_ms for item in observations
            ),
            "peak_buffer_memory_bytes": max(
                item.peak_buffer_memory_bytes for item in observations
            ),
            "peak_temp_directory_bytes": max(
                item.peak_temp_directory_bytes for item in observations
            ),
            "total_memory_allocated_bytes": max(
                item.total_memory_allocated_bytes for item in observations
            ),
            "boundary": boundaries[0],
        }
        connection.execute("DROP TABLE IF EXISTS ablation_output")
    fingerprints = {str(item["fingerprint"]) for item in profiles.values()}
    if len(fingerprints) != len(PIPELINE_ABLATION_VARIANTS):
        raise ValueError("Ablation variants did not produce four distinct physical plans")
    return profiles


def _run_scenario(
    connection: Any,
    config: PipelineAblationConfig,
    scenario: PipelineAblationScenario,
    *,
    run_id: str,
    commit_hash: str,
    output_dir: Path,
) -> dict[str, Any]:
    matched_rows = _create_data(connection, scenario)
    sql_by_variant = pipeline_ablation_sql()
    profiles = _profile_variants(
        connection, scenario, sql_by_variant, output_dir, config.profile_runs
    )
    orders = _variant_orders(
        config.warmup_runs + config.measured_runs,
        int.from_bytes(
            hashlib.sha256(
                f"{scenario.scenario_id}:{config.order_seed}".encode()
            ).digest()[:4],
            "big",
        ),
    )
    measurements: list[dict[str, Any]] = []
    all_digests: set[str] = set()
    for round_index, order in enumerate(orders):
        is_warmup = round_index < config.warmup_runs
        repeat_index = round_index - config.warmup_runs
        for position, variant in enumerate(order):
            latency_ms, checksum = _execute_variant(
                connection,
                scenario,
                variant,
                sql_by_variant[variant],
                matched_rows,
            )
            digest = _digest(checksum)
            all_digests.add(digest)
            if not is_warmup:
                measurements.append(
                    {
                        "run_id": run_id,
                        "commit_hash": commit_hash,
                        "scenario_id": scenario.scenario_id,
                        "region_label": scenario.region_label,
                        "row_count": scenario.row_count,
                        "identifier_width": scenario.identifier_width,
                        "match_rate": scenario.match_rate,
                        "matched_rows": matched_rows,
                        "seed": scenario.seed,
                        "repeat_index": repeat_index,
                        "order_position": position,
                        "variant": variant,
                        "latency_ms": latency_ms,
                        "result_digest": digest,
                        "physical_plan_fingerprint": profiles[variant]["fingerprint"],
                    }
                )
    if len(all_digests) != 1:
        raise ValueError(f"Ablation outputs differ for {scenario.scenario_id}")
    join_exact = True
    for variant, profile in profiles.items():
        cardinalities = [
            int(cardinality)
            for name, cardinality in zip(
                profile["operator_names"],
                profile["operator_cardinalities"],
                strict=True,
            )
            if name == "HASH_JOIN"
        ]
        if cardinalities != [matched_rows]:
            raise ValueError(f"{scenario.scenario_id}/{variant} Join cardinality differs")
    return {
        "scenario": asdict(scenario),
        "scenario_id": scenario.scenario_id,
        "matched_rows": matched_rows,
        "profiles": profiles,
        "measurements": measurements,
        "validation_passed": True,
        "validation_details": {
            "result_equivalent": True,
            "four_physical_plans_distinct": True,
            "boundaries_match_protocol": True,
            "join_cardinality_exact": join_exact,
        },
    }


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _finalize(output_dir: Path, config: PipelineAblationConfig, run_id: str) -> None:
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((output_dir / "units").glob("*.json"))
    ]
    measurements = [row for payload in payloads for row in payload["measurements"]]
    component_rows: list[dict[str, Any]] = []
    operator_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    for payload in payloads:
        scenario = payload["scenario"]
        by_variant: dict[str, list[dict[str, Any]]] = {}
        for row in payload["measurements"]:
            by_variant.setdefault(str(row["variant"]), []).append(row)
        for variant, rows in sorted(by_variant.items()):
            values = [float(row["latency_ms"]) for row in rows]
            profile = payload["profiles"][variant]
            exposure = pipeline_ablation_exposure(
                PipelineAblationScenario(**scenario), variant
            )
            component_rows.append(
                {
                    "scenario_id": payload["scenario_id"],
                    **scenario,
                    "matched_rows": payload["matched_rows"],
                    "variant": variant,
                    "runs": len(values),
                    "median_latency_ms": statistics.median(values),
                    "p95_latency_ms": _percentile95(values),
                    "min_latency_ms": min(values),
                    "max_latency_ms": max(values),
                    "physical_plan_fingerprint": profile["fingerprint"],
                    "peak_buffer_memory_bytes": profile["peak_buffer_memory_bytes"],
                    "peak_temp_directory_bytes": profile[
                        "peak_temp_directory_bytes"
                    ],
                    **asdict(exposure),
                }
            )
            boundary_rows.append(
                {
                    "scenario_id": payload["scenario_id"],
                    "variant": variant,
                    **profile["boundary"],
                    "physical_plan_fingerprint": profile["fingerprint"],
                }
            )
            for index, name in enumerate(profile["operator_names"]):
                operator_rows.append(
                    {
                        "scenario_id": payload["scenario_id"],
                        **scenario,
                        "variant": variant,
                        "operator_index": index,
                        "operator_name": name,
                        "median_operator_timing_ms": profile["operator_timings_ms"][
                            index
                        ],
                        "actual_cardinality": profile["operator_cardinalities"][index],
                        "rows_scanned": profile["rows_scanned"][index],
                        "physical_plan_fingerprint": profile["fingerprint"],
                    }
                )
    _write_csv(output_dir / "raw_measurements.csv", measurements)
    _write_csv(output_dir / "component_summary.csv", component_rows)
    _write_csv(output_dir / "operator_summary.csv", operator_rows)
    _write_csv(output_dir / "boundary_summary.csv", boundary_rows)
    temp_bytes = [
        int(profile["peak_temp_directory_bytes"])
        for payload in payloads
        for profile in payload["profiles"].values()
    ]
    spilled_units = {
        str(payload["scenario_id"])
        for payload in payloads
        if any(
            int(profile["peak_temp_directory_bytes"]) > 0
            for profile in payload["profiles"].values()
        )
    }
    _write_json_atomic(
        output_dir / "summary.json",
        {
            "run_id": run_id,
            "status": "complete",
            "evaluation_label": "phase2m_pipeline_ablation_smoke",
            "scenario_count": len(payloads),
            "variant_count": len(PIPELINE_ABLATION_VARIANTS),
            "measurement_count": len(measurements),
            "operator_summary_count": len(operator_rows),
            "all_validations_passed": all(
                payload.get("validation_passed") is True for payload in payloads
            ),
            "result_equivalent_scenario_count": sum(
                payload["validation_details"]["result_equivalent"] is True
                for payload in payloads
            ),
            "distinct_plan_scenario_count": sum(
                payload["validation_details"]["four_physical_plans_distinct"] is True
                for payload in payloads
            ),
            "boundary_validated_scenario_count": sum(
                payload["validation_details"]["boundaries_match_protocol"] is True
                for payload in payloads
            ),
            "exact_join_cardinality_scenario_count": sum(
                payload["validation_details"]["join_cardinality_exact"] is True
                for payload in payloads
            ),
            "exposure_annotated_component_count": len(component_rows),
            "spilled_profile_count": sum(value > 0 for value in temp_bytes),
            "spilled_scenario_count": len(spilled_units),
            "max_peak_temp_directory_bytes": max(temp_bytes, default=0),
            "compact_matrix_authorized": (
                len(payloads) == len(config.scenarios)
                and all(payload.get("validation_passed") is True for payload in payloads)
                and len(spilled_units) == 0
            ),
            "phase2g_authorized": False,
            "scientific_boundary": (
                "Smoke validates semantic equivalence and actual physical boundaries. "
                "Its few timings cannot support a performance or optimizer claim."
            ),
        },
    )


def run_pipeline_ablation_smoke(
    config: PipelineAblationConfig,
    *,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or safely resume the Phase 2M smoke on CPU-only DuckDB."""

    import duckdb

    root = _repo_root()
    commit_hash = _git_commit(root)
    git_dirty = _git_dirty(root)
    if config.require_clean_git and git_dirty:
        raise ValueError("Phase 2M smoke requires a clean Git worktree")
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
            raise ValueError(f"Cannot resume missing Phase 2M run: {resume_run_id}")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != config_digest:
            raise ValueError("Resume config differs from the Phase 2M run")
        environment = json.loads(
            (output_dir / "environment.json").read_text(encoding="utf-8")
        )
        if environment.get("commit_hash") != commit_hash:
            raise ValueError("Cannot resume Phase 2M after the Git commit changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_scenarios": [],
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write_json_atomic(output_dir / "config.json", config_payload)
        _write_json_atomic(
            output_dir / "environment.json", _environment(commit_hash, git_dirty, config)
        )
        _write_json_atomic(checkpoint_path, checkpoint)
        _write_json_atomic(results_root / "latest_run.json", {"run_id": run_id})
    completed = set(checkpoint["completed_scenarios"])
    started = time.perf_counter()
    session_completed = 0
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        for scenario in config.scenarios:
            if scenario.scenario_id in completed:
                continue
            try:
                payload = _run_scenario(
                    connection,
                    config,
                    scenario,
                    run_id=run_id,
                    commit_hash=commit_hash,
                    output_dir=output_dir,
                )
            except Exception as error:
                _write_json_atomic(
                    output_dir / "failures" / f"{scenario.scenario_id}.json",
                    {
                        "scenario_id": scenario.scenario_id,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
                raise
            _write_json_atomic(
                output_dir / "units" / f"{scenario.scenario_id}.json", payload
            )
            completed.add(scenario.scenario_id)
            session_completed += 1
            checkpoint["completed_scenarios"] = sorted(completed)
            checkpoint["updated_at"] = datetime.now(UTC).isoformat()
            _write_json_atomic(checkpoint_path, checkpoint)
            elapsed = time.perf_counter() - started
            done = len(completed)
            eta = elapsed / session_completed * (len(config.scenarios) - done)
            progress = {
                "run_id": run_id,
                "completed_scenarios": done,
                "total_scenarios": len(config.scenarios),
                "fraction": done / len(config.scenarios),
                "current_scenario": scenario.scenario_id,
                "elapsed_seconds": elapsed,
                "eta_seconds": eta,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            _write_json_atomic(output_dir / "progress.json", progress)
            _write_json_atomic(results_root / "latest_progress.json", progress)
            if show_progress:
                print(
                    f"[phase2m {done}/{len(config.scenarios)}] "
                    f"{scenario.scenario_id} elapsed={elapsed:.1f}s eta={eta:.1f}s",
                    flush=True,
                )
    finally:
        connection.close()
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    _write_json_atomic(checkpoint_path, checkpoint)
    _finalize(output_dir, config, run_id)
    return output_dir


def load_pipeline_ablation_config(path: str | Path) -> PipelineAblationConfig:
    """Load a versioned Phase 2M protocol from JSON."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Phase 2M config must contain a JSON object")
    raw_scenarios = payload.get("scenarios")
    raw_templates = payload.get("scenario_templates")
    raw_seeds = payload.get("seeds")
    if raw_scenarios is not None and raw_templates is not None:
        raise ValueError("Phase 2M config cannot mix scenarios and templates")
    if raw_templates is not None:
        if not isinstance(raw_templates, list) or not isinstance(raw_seeds, list):
            raise ValueError("Phase 2M templates require a seed list")
        expanded: list[dict[str, Any]] = []
        for template in raw_templates:
            if not isinstance(template, dict):
                raise ValueError("Phase 2M scenario template must be an object")
            item = cast(dict[str, Any], template)
            for raw_seed in raw_seeds:
                seed = int(raw_seed)
                expanded.append(
                    {
                        **item,
                        "scenario_id": f"{item['scenario_id_prefix']}-s{seed}",
                        "seed": seed,
                    }
                )
        raw_scenarios = expanded
    if not isinstance(raw_scenarios, list):
        raise ValueError("Phase 2M config requires scenarios or templates")
    scenarios = []
    for raw in raw_scenarios:
        if not isinstance(raw, dict):
            raise ValueError("Phase 2M scenario must contain an object")
        item = cast(dict[str, Any], raw)
        scenarios.append(
            PipelineAblationScenario(
                scenario_id=str(item["scenario_id"]),
                region_label=str(item["region_label"]),
                row_count=int(item["row_count"]),
                identifier_width=int(item["identifier_width"]),
                match_rate=float(item["match_rate"]),
                seed=int(item["seed"]),
            )
        )
    return PipelineAblationConfig(
        results_dir=str(payload["results_dir"]),
        scenarios=tuple(scenarios),
        warmup_runs=int(payload.get("warmup_runs", 1)),
        measured_runs=int(payload.get("measured_runs", 3)),
        profile_runs=int(payload.get("profile_runs", 1)),
        duckdb_threads=int(payload.get("duckdb_threads", 4)),
        duckdb_memory_limit_mb=int(payload.get("duckdb_memory_limit_mb", 4096)),
        order_seed=int(payload.get("order_seed", 20260718)),
        require_clean_git=bool(payload.get("require_clean_git", True)),
    )
