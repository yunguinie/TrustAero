"""Execution-flow audit for DuckDB governed candidate cost representation.

This experiment deliberately does not train or select an optimizer.  It asks
which columns and operators actually appear in DuckDB plans when a wide value
is pruned, hashed, materialized, aggregated, or sorted around the same Join.
Observed operator evidence is kept separate from logical-byte estimates so a
paper cannot accidentally describe estimated bytes as engine-reported bytes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, cast

from trustaero.execution import PhysicalPlanObservation, observe_duckdb_plan

KNOWN_PLAN_COLUMNS = (
    "row_id",
    "sensitive_value",
    "join_key",
    "dimension_key",
    "marker",
    "masked_value",
)


@dataclass(frozen=True, slots=True)
class ExecutionFlowAuditConfig:
    """Frozen dimensions and resource controls for the mechanism audit."""

    results_dir: str
    row_counts: tuple[int, ...]
    identifier_widths: tuple[int, ...]
    match_rates: tuple[float, ...]
    seeds: tuple[int, ...]
    variant_ids: tuple[str, ...]
    warmup_runs: int
    measured_runs: int
    profile_runs: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    require_clean_git: bool
    order_design: str = "latin_rotations"

    def __post_init__(self) -> None:
        if not self.results_dir:
            raise ValueError("Execution-flow results_dir cannot be empty")
        dimensions: tuple[tuple[object, ...], ...] = (
            cast(tuple[object, ...], self.row_counts),
            cast(tuple[object, ...], self.identifier_widths),
            cast(tuple[object, ...], self.match_rates),
            cast(tuple[object, ...], self.seeds),
            cast(tuple[object, ...], self.variant_ids),
        )
        if any(not values or len(values) != len(set(values)) for values in dimensions):
            raise ValueError("Execution-flow dimensions must be nonempty and unique")
        if any(value <= 0 for value in self.row_counts):
            raise ValueError("Execution-flow row counts must be positive")
        if any(not 1 <= value <= 4096 for value in self.identifier_widths):
            raise ValueError("Execution-flow widths must be in [1, 4096]")
        if any(not 0.0 <= value <= 1.0 for value in self.match_rates):
            raise ValueError("Execution-flow match rates must be in [0, 1]")
        if any(value < 0 for value in self.seeds):
            raise ValueError("Execution-flow seeds must be nonnegative")
        supported = {item.variant_id for item in execution_flow_variants()}
        if set(self.variant_ids) != supported:
            raise ValueError("Execution-flow configuration must retain the full EA-0 matrix")
        if self.warmup_runs < 0 or min(self.measured_runs, self.profile_runs) < 1:
            raise ValueError("Execution-flow repetition counts are invalid")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("Execution-flow DuckDB limits are invalid")
        if self.order_design not in {"latin_rotations", "balanced_carryover"}:
            raise ValueError("Unsupported execution-flow order design")
        if self.order_design == "balanced_carryover":
            # An odd number of candidates needs one neutral barrier period so
            # that an even-order Williams design can balance direct carryover.
            design_size = len(self.variant_ids) + len(self.variant_ids) % 2
            if self.measured_runs % design_size:
                raise ValueError(
                    "Carryover-balanced measured runs must be a multiple of "
                    f"the augmented design size ({design_size})"
                )


@dataclass(frozen=True, slots=True)
class ExecutionFlowUnit:
    """One indivisible rows-width-match-seed mechanism unit."""

    row_count: int
    identifier_width: int
    match_rate: float
    seed: int

    @property
    def matched_rows(self) -> int:
        return round(self.row_count * self.match_rate)

    @property
    def unit_id(self) -> str:
        match = round(self.match_rate * 1000)
        return f"flow-n{self.row_count}-w{self.identifier_width}-m{match:04d}-s{self.seed}"


@dataclass(frozen=True, slots=True)
class ExecutionFlowVariant:
    """One SQL realization and its independently reviewable physical intent."""

    variant_id: str
    equivalence_group: str
    sql: str
    output_kind: str
    mask_placement: str
    materialization_boundary: str
    aggregation: bool
    sorting: bool
    expected_sensitive_column_in_plan: bool
    evaluation_role: Literal["mechanism_only", "deployable"] = "deployable"


@dataclass(frozen=True, slots=True)
class PhysicalWorkVector:
    """Estimated candidate work; every field is a logical estimate, not telemetry."""

    scan_rows: int
    join_build_rows: int
    join_probe_rows: int
    join_output_rows: int
    join_key_width_bytes: int
    estimated_sensitive_scan_bytes: int
    estimated_mask_rows: int
    estimated_mask_input_bytes: int
    estimated_raw_materialization_bytes: int
    estimated_masked_materialization_bytes: int
    estimated_sort_rows: int
    estimated_sort_key_bytes: int
    estimated_output_rows: int
    estimated_output_payload_bytes: int
    lineage_rows: int = 0


def execution_flow_variants() -> tuple[ExecutionFlowVariant, ...]:
    """Return the fixed EA-0 matrix; variants within a group are result-equivalent."""

    variants = (
        ExecutionFlowVariant(
            "join_key_only_aggregate",
            "column_pruning",
            "CREATE TEMP TABLE flow_output AS "
            "SELECT count(*)::BIGINT AS result_rows, "
            "sum(dimension.marker)::HUGEINT AS marker_sum, "
            "count(*)::HUGEINT * {identifier_width} AS payload_length_sum "
            "FROM flow_events AS events INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key",
            "aggregate",
            "none",
            "none",
            True,
            False,
            False,
            "mechanism_only",
        ),
        ExecutionFlowVariant(
            "dead_raw_projection_aggregate",
            "column_pruning",
            "CREATE TEMP TABLE flow_output AS "
            "WITH joined AS ("
            "SELECT events.sensitive_value, dimension.marker "
            "FROM flow_events AS events INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key"
            ") SELECT count(*)::BIGINT AS result_rows, "
            "sum(marker)::HUGEINT AS marker_sum, "
            "count(*)::HUGEINT * {identifier_width} AS payload_length_sum "
            "FROM joined",
            "aggregate",
            "none",
            "optimizer_prunable",
            True,
            False,
            False,
            "mechanism_only",
        ),
        ExecutionFlowVariant(
            "raw_materialized_aggregate",
            "column_pruning",
            "CREATE TEMP TABLE flow_output AS "
            "WITH joined AS MATERIALIZED ("
            "SELECT events.sensitive_value, dimension.marker "
            "FROM flow_events AS events INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key"
            ") SELECT count(*)::BIGINT AS result_rows, "
            "sum(marker)::HUGEINT AS marker_sum, "
            "sum(length(sensitive_value))::HUGEINT AS payload_length_sum "
            "FROM joined",
            "aggregate",
            "none",
            "raw_after_join",
            True,
            False,
            True,
            "mechanism_only",
        ),
        ExecutionFlowVariant(
            "prejoin_mask_materialized_output",
            "mask_output",
            "CREATE TEMP TABLE flow_output AS "
            "WITH masked AS MATERIALIZED ("
            "SELECT row_id, sha256(sensitive_value) AS masked_value, join_key "
            "FROM flow_events"
            ") SELECT masked.row_id, masked.masked_value, dimension.marker "
            "FROM masked INNER JOIN flow_dimension AS dimension "
            "ON masked.join_key = dimension.dimension_key",
            "masked_rows",
            "before_join",
            "masked_before_join",
            False,
            False,
            True,
        ),
        ExecutionFlowVariant(
            "postjoin_mask_fused_output",
            "mask_output",
            "CREATE TEMP TABLE flow_output AS "
            "SELECT events.row_id, sha256(events.sensitive_value) AS masked_value, "
            "dimension.marker FROM flow_events AS events "
            "INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key",
            "masked_rows",
            "after_join",
            "none",
            False,
            False,
            True,
        ),
        ExecutionFlowVariant(
            "postjoin_raw_materialized_mask_output",
            "mask_output",
            "CREATE TEMP TABLE flow_output AS "
            "WITH joined AS MATERIALIZED ("
            "SELECT events.row_id, events.sensitive_value, dimension.marker "
            "FROM flow_events AS events INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key"
            ") SELECT row_id, sha256(sensitive_value) AS masked_value, marker "
            "FROM joined",
            "masked_rows",
            "after_join",
            "raw_after_join",
            False,
            False,
            True,
        ),
        ExecutionFlowVariant(
            "prejoin_mask_materialized_aggregate",
            "mask_aggregate",
            "CREATE TEMP TABLE flow_output AS "
            "WITH masked AS MATERIALIZED ("
            "SELECT sha256(sensitive_value) AS masked_value, join_key "
            "FROM flow_events"
            ") SELECT count(*)::BIGINT AS result_rows, "
            "sum(dimension.marker)::HUGEINT AS marker_sum, "
            "bit_xor(hash(masked.masked_value)) AS mask_digest "
            "FROM masked INNER JOIN flow_dimension AS dimension "
            "ON masked.join_key = dimension.dimension_key",
            "masked_aggregate",
            "before_join",
            "masked_before_join",
            True,
            False,
            True,
        ),
        ExecutionFlowVariant(
            "postjoin_mask_fused_aggregate",
            "mask_aggregate",
            "CREATE TEMP TABLE flow_output AS "
            "SELECT count(*)::BIGINT AS result_rows, "
            "sum(dimension.marker)::HUGEINT AS marker_sum, "
            "bit_xor(hash(sha256(events.sensitive_value))) AS mask_digest "
            "FROM flow_events AS events INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key",
            "masked_aggregate",
            "after_join",
            "none",
            True,
            False,
            True,
        ),
        ExecutionFlowVariant(
            "postjoin_raw_materialized_mask_aggregate",
            "mask_aggregate",
            "CREATE TEMP TABLE flow_output AS "
            "WITH joined AS MATERIALIZED ("
            "SELECT events.sensitive_value, dimension.marker "
            "FROM flow_events AS events INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key"
            ") SELECT count(*)::BIGINT AS result_rows, "
            "sum(marker)::HUGEINT AS marker_sum, "
            "bit_xor(hash(sha256(sensitive_value))) AS mask_digest FROM joined",
            "masked_aggregate",
            "after_join",
            "raw_after_join",
            True,
            False,
            True,
        ),
        ExecutionFlowVariant(
            "prejoin_mask_materialized_sorted_output",
            "mask_sorted_output",
            "CREATE TEMP TABLE flow_output AS "
            "WITH masked AS MATERIALIZED ("
            "SELECT row_id, sha256(sensitive_value) AS masked_value, join_key "
            "FROM flow_events"
            ") SELECT masked.row_id, masked.masked_value, dimension.marker "
            "FROM masked INNER JOIN flow_dimension AS dimension "
            "ON masked.join_key = dimension.dimension_key "
            "ORDER BY masked.masked_value, masked.row_id",
            "masked_rows",
            "before_join",
            "masked_before_join",
            False,
            True,
            True,
        ),
        ExecutionFlowVariant(
            "postjoin_mask_fused_sorted_output",
            "mask_sorted_output",
            "CREATE TEMP TABLE flow_output AS "
            "SELECT events.row_id, sha256(events.sensitive_value) AS masked_value, "
            "dimension.marker FROM flow_events AS events "
            "INNER JOIN flow_dimension AS dimension "
            "ON events.join_key = dimension.dimension_key "
            "ORDER BY masked_value, events.row_id",
            "masked_rows",
            "after_join",
            "none",
            False,
            True,
            True,
        ),
    )
    if len({item.variant_id for item in variants}) != len(variants):
        raise AssertionError("Execution-flow variant IDs must remain unique")
    return variants


def execution_flow_units(
    config: ExecutionFlowAuditConfig,
) -> tuple[ExecutionFlowUnit, ...]:
    """Expand complete mechanism units in deterministic configuration order."""

    return tuple(
        ExecutionFlowUnit(rows, width, match, seed)
        for rows in config.row_counts
        for width in config.identifier_widths
        for match in config.match_rates
        for seed in config.seeds
    )


def physical_work_vector(
    unit: ExecutionFlowUnit,
    variant: ExecutionFlowVariant,
) -> PhysicalWorkVector:
    """Build the explicit estimated work vector for one legal SQL variant."""

    matched = unit.matched_rows
    build_rows = min(unit.row_count, 10_000)
    before = variant.mask_placement == "before_join"
    after = variant.mask_placement == "after_join"
    raw_boundary = variant.materialization_boundary == "raw_after_join"
    masked_boundary = variant.materialization_boundary == "masked_before_join"
    needs_sensitive = variant.expected_sensitive_column_in_plan
    mask_rows = unit.row_count if before else matched if after else 0
    output_rows = matched if variant.output_kind == "masked_rows" else 1
    if output_rows == matched:
        output_payload = matched * (8 + 64 + 8)
    elif variant.output_kind == "masked_aggregate":
        output_payload = 32
    elif variant.output_kind == "aggregate":
        output_payload = 40
    else:
        raise ValueError(f"Unknown output kind: {variant.output_kind}")
    raw_boundary_width = unit.identifier_width + (
        16 if variant.variant_id == "postjoin_raw_materialized_mask_output" else 8
    )
    masked_boundary_width = 64 + 8 + (8 if variant.output_kind == "masked_rows" else 0)
    return PhysicalWorkVector(
        scan_rows=unit.row_count,
        join_build_rows=build_rows,
        join_probe_rows=unit.row_count,
        join_output_rows=matched,
        join_key_width_bytes=8,
        estimated_sensitive_scan_bytes=(
            unit.row_count * unit.identifier_width if needs_sensitive else 0
        ),
        estimated_mask_rows=mask_rows,
        estimated_mask_input_bytes=mask_rows * unit.identifier_width,
        estimated_raw_materialization_bytes=(matched * raw_boundary_width if raw_boundary else 0),
        estimated_masked_materialization_bytes=(
            unit.row_count * masked_boundary_width if masked_boundary else 0
        ),
        estimated_sort_rows=matched if variant.sorting else 0,
        estimated_sort_key_bytes=matched * (64 + 8) if variant.sorting else 0,
        estimated_output_rows=output_rows,
        estimated_output_payload_bytes=output_payload,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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


def _digest(value: object) -> str:
    encoded = json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    # Windows virus scanners can briefly retain a checkpoint handle. Retry
    # only this bounded atomic replacement; other failures still surface.
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (2**attempt))


def _environment(commit: str, dirty: bool, config: ExecutionFlowAuditConfig) -> dict[str, object]:
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


def _create_data(connection: Any, unit: ExecutionFlowUnit) -> tuple[int, int]:
    """Create deterministic exact-width fact data and a fixed-size Join build side."""

    for table in ("flow_output", "flow_events", "flow_dimension"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    blocks = math.ceil(unit.identifier_width / 32)
    dimension_rows = min(unit.row_count, 10_000)
    connection.execute(
        f"""
        CREATE TABLE flow_events AS
        SELECT
            i::BIGINT AS row_id,
            left(
                repeat(md5(CAST(i + {unit.seed * 1_000_003} AS VARCHAR)), {blocks}),
                {unit.identifier_width}
            ) AS sensitive_value,
            CASE
                WHEN i < {unit.matched_rows} THEN (i % {dimension_rows})::BIGINT
                ELSE ({dimension_rows} + i)::BIGINT
            END AS join_key
        FROM range({unit.row_count}) AS source(i)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE flow_dimension AS
        SELECT i::BIGINT AS dimension_key, (i % 97)::BIGINT AS marker
        FROM range({dimension_rows}) AS source(i)
        """
    )
    observed = connection.execute(
        "SELECT count(*), min(length(sensitive_value)), "
        "max(length(sensitive_value)) FROM flow_events"
    ).fetchone()
    expected = (unit.row_count, unit.identifier_width, unit.identifier_width)
    if observed != expected:
        raise ValueError(f"Execution-flow data failed width validation: {unit.unit_id}")
    marker_sum = connection.execute(
        "SELECT sum(dimension.marker)::HUGEINT FROM flow_events AS events "
        "INNER JOIN flow_dimension AS dimension "
        "ON events.join_key = dimension.dimension_key"
    ).fetchone()
    if marker_sum is None:
        raise ValueError("Execution-flow marker checksum setup failed")
    return unit.matched_rows, int(marker_sum[0] or 0)


def _plan_nodes(plan_json: str) -> list[dict[str, Any]]:
    raw = json.loads(plan_json)
    output: list[dict[str, Any]] = []

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if "operator_name" in node or "name" in node:
            output.append(node)
        for child in cast(list[object], node.get("children", [])):
            visit(child)

    visit(raw)
    return output


def observed_operator_columns(plan_json: str) -> tuple[tuple[str, ...], ...]:
    """Extract only column names explicitly exposed in each operator's metadata."""

    output: list[tuple[str, ...]] = []
    for node in _plan_nodes(plan_json):
        extra = json.dumps(node.get("extra_info", {}), sort_keys=True)
        output.append(tuple(name for name in KNOWN_PLAN_COLUMNS if name in extra))
    return tuple(output)


def _profile_variant(
    connection: Any,
    unit: ExecutionFlowUnit,
    variant: ExecutionFlowVariant,
    *,
    profile_runs: int,
    plan_dir: Path,
) -> dict[str, object]:
    observations: list[PhysicalPlanObservation] = []
    observed_columns: list[tuple[tuple[str, ...], ...]] = []
    plan_dir.mkdir(parents=True, exist_ok=True)
    sql = _render_sql(variant, unit)
    for index in range(profile_runs):
        connection.execute("DROP TABLE IF EXISTS flow_output")
        observation = observe_duckdb_plan(connection, sql, analyze=True)
        observations.append(observation)
        columns = observed_operator_columns(observation.plan_json)
        observed_columns.append(columns)
        (plan_dir / f"{variant.variant_id}-analyze-r{index}.json").write_text(
            observation.plan_json + "\n", encoding="utf-8"
        )
    if len({item.fingerprint for item in observations}) != 1:
        raise ValueError(f"Physical plan changed within profiles: {variant.variant_id}")
    if len({item.operator_names for item in observations}) != 1:
        raise ValueError(f"Operator shape changed within profiles: {variant.variant_id}")
    if len(set(observed_columns)) != 1:
        raise ValueError(f"Observed columns changed within profiles: {variant.variant_id}")
    reference = observations[0]
    column_union = sorted({name for values in observed_columns[0] for name in values})
    return {
        "variant_id": variant.variant_id,
        "fingerprint": reference.fingerprint,
        "operator_names": list(reference.operator_names),
        "operator_columns": [list(values) for values in observed_columns[0]],
        "observed_column_union": column_union,
        "operator_timings_ms": [
            statistics.median(item.operator_timings_ms[index] for item in observations)
            for index in range(len(reference.operator_names))
        ],
        "operator_cardinalities": list(reference.actual_cardinalities),
        "rows_scanned": list(reference.rows_scanned),
        "profile_latency_ms": statistics.median(item.profile_latency_ms for item in observations),
        "peak_buffer_memory_bytes": max(item.peak_buffer_memory_bytes for item in observations),
        "peak_temp_directory_bytes": max(item.peak_temp_directory_bytes for item in observations),
        "total_memory_allocated_bytes": max(
            item.total_memory_allocated_bytes for item in observations
        ),
        "profile_runs": profile_runs,
    }


def _output_checksum(connection: Any, output_kind: str) -> tuple[object, ...]:
    if output_kind == "aggregate":
        row = connection.execute(
            "SELECT result_rows, marker_sum, payload_length_sum FROM flow_output"
        ).fetchone()
    elif output_kind == "masked_aggregate":
        row = connection.execute(
            "SELECT result_rows, marker_sum, mask_digest FROM flow_output"
        ).fetchone()
    elif output_kind == "masked_rows":
        row = connection.execute(
            "SELECT count(*)::BIGINT, sum(length(masked_value))::HUGEINT, "
            "sum(row_id)::HUGEINT, sum(marker)::HUGEINT, "
            "bit_xor(hash(row_id, masked_value, marker)) FROM flow_output"
        ).fetchone()
    else:
        raise ValueError(f"Unsupported execution-flow output kind: {output_kind}")
    if row is None:
        raise ValueError("Execution-flow output checksum returned no row")
    return tuple(row)


def _render_sql(variant: ExecutionFlowVariant, unit: ExecutionFlowUnit) -> str:
    """Substitute only frozen numeric mechanism constants into a variant."""

    return variant.sql.replace("{identifier_width}", str(unit.identifier_width))


def _execute_variant(
    connection: Any,
    unit: ExecutionFlowUnit,
    variant: ExecutionFlowVariant,
    *,
    repeat_index: int,
    order_position: int,
    is_warmup: bool,
) -> dict[str, object]:
    connection.execute("DROP TABLE IF EXISTS flow_output")
    started = time.perf_counter()
    connection.execute(_render_sql(variant, unit))
    latency_ms = (time.perf_counter() - started) * 1000.0
    checksum = _output_checksum(connection, variant.output_kind)
    if int(cast(Any, checksum[0])) != unit.matched_rows:
        raise ValueError(f"Join cardinality mismatch: {unit.unit_id}/{variant.variant_id}")
    return {
        "unit_id": unit.unit_id,
        "variant_id": variant.variant_id,
        "equivalence_group": variant.equivalence_group,
        "repeat_index": repeat_index,
        "order_position": order_position,
        "is_warmup": is_warmup,
        "latency_ms": latency_ms,
        "result_digest": _digest(checksum),
    }


def _variant_orders(
    variant_ids: Sequence[str], rounds: int, *, seed: int
) -> tuple[tuple[str, ...], ...]:
    """Use deterministic shuffled Latin rotations to balance order positions."""

    base = list(variant_ids)
    random.Random(seed).shuffle(base)
    return tuple(
        tuple(base[(index + offset) % len(base)] for index in range(len(base)))
        for offset in range(rounds)
    )


_ORDER_BARRIER = "__trustaero_measurement_barrier__"


def _balanced_carryover_orders(
    variant_ids: Sequence[str], rounds: int, *, seed: int
) -> tuple[tuple[str, ...], ...]:
    """Balance both position and the immediately preceding candidate.

    Plain cyclic rotations balance positions but preserve the same predecessor
    for every candidate.  That can confound latency when one DuckDB plan leaves
    cache, allocator, or temporary-state effects for the next plan.  A Williams
    design uses a special column order whose adjacent modular differences cover
    every nonzero value exactly once.  For an odd candidate count, a lightweight
    SQL barrier supplies the required even design size.
    """

    symbols = list(variant_ids)
    if len(symbols) % 2:
        symbols.append(_ORDER_BARRIER)
    size = len(symbols)
    if size < 2 or size % 2:
        raise ValueError("Carryover-balanced design requires an even size")
    if rounds % size:
        raise ValueError(f"Carryover-balanced rounds must be a multiple of {size}")

    randomizer = random.Random(seed)
    randomizer.shuffle(symbols)
    # 0, 1, n-1, 2, n-2, ... is a directed terrace for even cyclic n.
    columns = [0]
    for step in range(1, size):
        columns.append((step + 1) // 2 if step % 2 else size - step // 2)

    orders: list[tuple[str, ...]] = []
    for block_index in range(rounds // size):
        offsets = list(range(size))
        random.Random(seed + block_index + 1).shuffle(offsets)
        orders.extend(
            tuple(symbols[(column + offset) % size] for column in columns) for offset in offsets
        )
    return tuple(orders)


def _validate_unit(
    variants: Sequence[ExecutionFlowVariant],
    profiles: dict[str, dict[str, object]],
    measurements: list[dict[str, object]],
) -> dict[str, object]:
    groups: dict[str, set[str]] = {}
    for row in measurements:
        if bool(row["is_warmup"]):
            continue
        groups.setdefault(str(row["equivalence_group"]), set()).add(str(row["result_digest"]))
    if any(len(values) != 1 for values in groups.values()):
        raise ValueError("Execution-flow equivalent variants returned different results")
    sensitive = {
        item.variant_id: "sensitive_value"
        in cast(list[str], profiles[item.variant_id]["observed_column_union"])
        for item in variants
    }
    if sensitive["join_key_only_aggregate"]:
        raise ValueError("Key-only plan unexpectedly retained the sensitive column")
    if sensitive["dead_raw_projection_aggregate"]:
        raise ValueError("DuckDB did not prune the unused sensitive projection")
    if not sensitive["raw_materialized_aggregate"]:
        raise ValueError("Raw materialization plan did not retain the sensitive column")
    fingerprints = {item: str(profile["fingerprint"]) for item, profile in profiles.items()}
    distinct_pairs = (
        (
            "join_key_only_aggregate",
            "raw_materialized_aggregate",
        ),
        (
            "prejoin_mask_materialized_output",
            "postjoin_mask_fused_output",
        ),
        (
            "prejoin_mask_materialized_aggregate",
            "postjoin_mask_fused_aggregate",
        ),
    )
    if any(fingerprints[left] == fingerprints[right] for left, right in distinct_pairs):
        raise ValueError("Execution-flow audit lost an expected physical-plan distinction")
    return {
        "result_equivalence_groups_passed": sorted(groups),
        "unused_sensitive_projection_pruned": True,
        "materialized_sensitive_projection_retained": True,
        "expected_physical_plan_pairs_distinct": True,
        "engine_reported_bytes_available": False,
        "byte_evidence_boundary": (
            "logical work bytes are estimates; DuckDB reports memory and temporary "
            "directory peaks but not per-operator payload bytes"
        ),
    }


def _run_unit(
    connection: Any,
    config: ExecutionFlowAuditConfig,
    unit: ExecutionFlowUnit,
    variants: Sequence[ExecutionFlowVariant],
    *,
    output_dir: Path,
) -> dict[str, object]:
    matched_rows, marker_sum = _create_data(connection, unit)
    profiles = {
        item.variant_id: _profile_variant(
            connection,
            unit,
            item,
            profile_runs=config.profile_runs,
            plan_dir=output_dir / "plans" / unit.unit_id,
        )
        for item in variants
    }
    order_seed = int.from_bytes(
        hashlib.sha256(f"{unit.unit_id}:{config.order_seed}".encode()).digest()[:8],
        "big",
    )
    variant_ids = [item.variant_id for item in variants]
    warmup_orders = _variant_orders(variant_ids, config.warmup_runs, seed=order_seed)
    if config.order_design == "balanced_carryover":
        measured_orders = _balanced_carryover_orders(
            variant_ids, config.measured_runs, seed=order_seed + 1
        )
    else:
        measured_orders = _variant_orders(
            variant_ids, config.measured_runs, seed=order_seed + config.warmup_runs
        )
    orders = warmup_orders + measured_orders
    by_id = {item.variant_id: item for item in variants}
    measurements: list[dict[str, object]] = []
    for round_index, order in enumerate(orders):
        warmup = round_index < config.warmup_runs
        if not warmup and config.order_design == "balanced_carryover":
            # Break the direct SQL predecessor at round boundaries.  Otherwise
            # the final candidate of one row would become an unbalanced extra
            # predecessor for the first candidate of the next row.
            connection.execute("SELECT 1").fetchone()
        for position, variant_id in enumerate(order):
            if variant_id == _ORDER_BARRIER:
                # The barrier is intentionally cheap and identical everywhere;
                # it occupies one period but is not a measured candidate.
                connection.execute("SELECT 1").fetchone()
                continue
            measurements.append(
                _execute_variant(
                    connection,
                    unit,
                    by_id[variant_id],
                    repeat_index=round_index - config.warmup_runs,
                    order_position=position,
                    is_warmup=warmup,
                )
            )
    validation = _validate_unit(variants, profiles, measurements)
    return {
        "unit": asdict(unit),
        "unit_id": unit.unit_id,
        "matched_rows": matched_rows,
        "marker_sum": marker_sum,
        "work_vectors": {
            item.variant_id: asdict(physical_work_vector(unit, item)) for item in variants
        },
        "profiles": profiles,
        "measurements": measurements,
        "validation": validation,
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"Execution-flow CSV cannot be empty: {path.name}")
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _finalize(
    output_dir: Path,
    config: ExecutionFlowAuditConfig,
    variants: Sequence[ExecutionFlowVariant],
) -> None:
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((output_dir / "units").glob("*.json"))
    ]
    expected_units = len(execution_flow_units(config))
    if len(payloads) != expected_units:
        raise ValueError("Execution-flow finalization found incomplete units")
    measurement_rows: list[dict[str, object]] = []
    variant_rows: list[dict[str, object]] = []
    operator_rows: list[dict[str, object]] = []
    for payload in payloads:
        unit = cast(dict[str, object], payload["unit"])
        for row in cast(list[dict[str, object]], payload["measurements"]):
            if not bool(row["is_warmup"]):
                measurement_rows.append({**unit, **row})
        profiles = cast(dict[str, dict[str, object]], payload["profiles"])
        work = cast(dict[str, dict[str, object]], payload["work_vectors"])
        for variant in variants:
            profile = profiles[variant.variant_id]
            selected = [
                row
                for row in cast(list[dict[str, object]], payload["measurements"])
                if row["variant_id"] == variant.variant_id and not row["is_warmup"]
            ]
            latencies = [float(cast(Any, row["latency_ms"])) for row in selected]
            variant_rows.append(
                {
                    **unit,
                    "unit_id": payload["unit_id"],
                    "variant_id": variant.variant_id,
                    "equivalence_group": variant.equivalence_group,
                    "mask_placement": variant.mask_placement,
                    "materialization_boundary": variant.materialization_boundary,
                    "aggregation": variant.aggregation,
                    "sorting": variant.sorting,
                    "median_latency_ms": statistics.median(latencies),
                    "p95_latency_ms": _p95(latencies),
                    "physical_plan_fingerprint": profile["fingerprint"],
                    "operator_names": "|".join(cast(list[str], profile["operator_names"])),
                    "observed_column_union": "|".join(
                        cast(list[str], profile["observed_column_union"])
                    ),
                    "peak_buffer_memory_bytes": profile["peak_buffer_memory_bytes"],
                    "peak_temp_directory_bytes": profile["peak_temp_directory_bytes"],
                    **work[variant.variant_id],
                }
            )
            names = cast(list[str], profile["operator_names"])
            columns = cast(list[list[str]], profile["operator_columns"])
            timings = cast(list[float], profile["operator_timings_ms"])
            cardinalities = cast(list[int], profile["operator_cardinalities"])
            rows_scanned = cast(list[int], profile["rows_scanned"])
            for index, name in enumerate(names):
                operator_rows.append(
                    {
                        **unit,
                        "unit_id": payload["unit_id"],
                        "variant_id": variant.variant_id,
                        "operator_index": index,
                        "operator_name": name,
                        "observed_columns": "|".join(columns[index]),
                        "median_operator_timing_ms": timings[index],
                        "actual_cardinality": cardinalities[index],
                        "rows_scanned": rows_scanned[index],
                        "physical_plan_fingerprint": profile["fingerprint"],
                    }
                )
    _write_csv(output_dir / "measurements.csv", measurement_rows)
    _write_csv(output_dir / "variant_summary.csv", variant_rows)
    _write_csv(output_dir / "operator_summary.csv", operator_rows)
    _atomic_json(
        output_dir / "summary.json",
        {
            "status": "PASS_EXECUTION_FLOW_AUDIT",
            "unit_count": expected_units,
            "variant_count": len(variants),
            "equivalence_group_count": len({item.equivalence_group for item in variants}),
            "measurement_count": len(measurement_rows),
            "profile_execution_count": expected_units * len(variants) * config.profile_runs,
            "all_units_validated": True,
            "engine_reported_per_operator_payload_bytes": False,
            "logical_work_bytes_are_estimates": True,
            "optimizer_trained": False,
            "scientific_boundary": (
                "EA-0 identifies DuckDB plan shape, active-column metadata, "
                "cardinality, operator time, memory, and spill behavior. It does not "
                "claim exact engine-internal payload bytes or optimizer performance."
            ),
        },
    )


def run_execution_flow_audit(
    config: ExecutionFlowAuditConfig,
    *,
    project_root: Path | None = None,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume EA-0 with atomic complete-unit checkpoints."""

    import duckdb

    root = (project_root or _repo_root()).resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Execution-flow audit requires a clean Git commit")
    variants_by_id = {item.variant_id: item for item in execution_flow_variants()}
    variants = tuple(variants_by_id[item] for item in config.variant_ids)
    units = execution_flow_units(config)
    results_root = root / config.results_dir
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = results_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    config_payload = json.loads(json.dumps(asdict(config), sort_keys=True))
    config_digest = _digest(config_payload)
    checkpoint_path = output_dir / "checkpoint.json"
    if resume_run_id:
        if not checkpoint_path.is_file():
            raise ValueError(f"Cannot resume missing execution-flow run: {run_id}")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        environment = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != config_digest:
            raise ValueError("Execution-flow resume configuration changed")
        if environment.get("commit_hash") != commit:
            raise ValueError("Execution-flow resume commit changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_units": [],
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
        }
        _atomic_json(output_dir / "config.json", config_payload)
        _atomic_json(output_dir / "environment.json", _environment(commit, dirty, config))
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    completed = set(cast(list[str], checkpoint["completed_units"]))
    started = time.perf_counter()
    session_completed = 0
    temp_dir = output_dir / "duckdb_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        escaped_temp = str(temp_dir.resolve()).replace("'", "''")
        connection.execute(f"SET temp_directory = '{escaped_temp}'")
        for unit in units:
            if unit.unit_id in completed:
                continue
            try:
                payload = _run_unit(connection, config, unit, variants, output_dir=output_dir)
            except Exception as error:
                _atomic_json(
                    output_dir / "failures" / f"{unit.unit_id}.json",
                    {
                        "unit_id": unit.unit_id,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
                raise
            _atomic_json(output_dir / "units" / f"{unit.unit_id}.json", payload)
            completed.add(unit.unit_id)
            checkpoint["completed_units"] = sorted(completed)
            checkpoint["updated_at"] = datetime.now(UTC).isoformat()
            _atomic_json(checkpoint_path, checkpoint)
            session_completed += 1
            elapsed = time.perf_counter() - started
            remaining = len(units) - len(completed)
            eta = elapsed / session_completed * remaining
            progress = {
                "run_id": run_id,
                "completed_units": len(completed),
                "total_units": len(units),
                "current_unit": unit.unit_id,
                "elapsed_seconds": elapsed,
                "eta_seconds": eta,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            _atomic_json(output_dir / "progress.json", progress)
            _atomic_json(results_root / "latest_progress.json", progress)
            if progress_callback is not None:
                progress_callback(len(completed), len(units), unit.unit_id, elapsed)
    finally:
        connection.close()
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    _atomic_json(checkpoint_path, checkpoint)
    _finalize(output_dir, config, variants)
    return output_dir


def load_execution_flow_audit_config(
    path: str | Path,
) -> ExecutionFlowAuditConfig:
    """Load a versioned EA-0 JSON protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ExecutionFlowAuditConfig(
        results_dir=str(payload["results_dir"]),
        row_counts=tuple(int(item) for item in payload["row_counts"]),
        identifier_widths=tuple(int(item) for item in payload["identifier_widths"]),
        match_rates=tuple(float(item) for item in payload["match_rates"]),
        seeds=tuple(int(item) for item in payload["seeds"]),
        variant_ids=tuple(str(item) for item in payload["variant_ids"]),
        warmup_runs=int(payload["warmup_runs"]),
        measured_runs=int(payload["measured_runs"]),
        profile_runs=int(payload["profile_runs"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        require_clean_git=bool(payload["require_clean_git"]),
        order_design=str(payload.get("order_design", "latin_rotations")),
    )
