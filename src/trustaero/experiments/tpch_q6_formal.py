"""Frozen paired timing protocol for governed TPC-H SF1 Q6.

Three candidates require six execution-order permutations. The development
protocol repeats every permutation five times, producing 30 paired blocks and
90 timed executions. It is standard-benchmark method evidence, not an unseen
optimizer evaluation.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data.download import sha256_file
from trustaero.execution import (
    CompiledQuery,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.paired_claims import assess_carryover, authorize_paired_claims
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_candidates import verify_candidate_execution_certificate
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _Progress
from trustaero.experiments.tpch_audit import tpch_git_state, verify_tpch_artifact
from trustaero.experiments.tpch_q6 import (
    TPCH_Q6_MATERIALIZATION_TARGETS,
    create_tpch_q6_binding,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility import audit_source_freeze
from trustaero.validator.service import validate

TPCH_Q6_FORMAL_LABEL = "tpch_sf1_q6_formal_development_v1"
TPCH_Q6_BATCHED_LABEL = "tpch_sf1_q6_batched_median_development_v2"
TPCH_Q6_UTC_BATCHED_LABEL = "tpch_sf1_q6_exact_decimal_utc_batched_v3"
TPCH_Q6_SF10_FORMAL_LABEL = "tpch_sf10_q6_exact_decimal_utc_batched_v1"
TPCH_Q6_SF10_PAIRED_CI_LABEL = "tpch_sf10_q6_pollution_safe_paired_ci_v2"


@dataclass(frozen=True, slots=True)
class TpchQ6FormalConfig:
    results_dir: str
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    absolute_half_drift_limit: float
    paired_ratio_half_drift_limit: float
    paired_ratio_outlier_fraction_limit: float
    tie_threshold_fraction: float
    artifact_sha256: str
    semantic_smoke_sha256: str
    support_audit_sha256: str
    scale_factor: int = 1
    require_clean_git: bool = True
    scientific_label: str = TPCH_Q6_FORMAL_LABEL
    paper_performance_evidence: bool = True
    heldout_optimizer_evidence: bool = False
    timing_protocol: str = "single_observation_v1"
    timed_repeats_per_position: int = 1
    duckdb_timezone: str = "UTC"
    semantic_smoke_path: str = "results/tpch_q6_semantic_smoke/result.json"
    support_audit_path: str = "results/tpch_sf1_support_audit/audit.json"
    carryover_candidate_ids: tuple[str, ...] = ()
    carryover_tolerance_fraction: float = 0.1
    confidence_level: float = 0.95
    bootstrap_repetitions: int = 10000
    bootstrap_seed: int = 20260726
    minimum_carryover_pairs: int = 5
    minimum_claim_blocks: int = 10

    def __post_init__(self) -> None:
        if (
            not self.results_dir
            or not self.require_clean_git
            or not self.paper_performance_evidence
            or self.heldout_optimizer_evidence
        ):
            raise ValueError("formal TPC-H Q6 scope is invalid")
        protocol_shape = {
            "single_observation_v1": (TPCH_Q6_FORMAL_LABEL, 1),
            "batched_median_v2": (TPCH_Q6_BATCHED_LABEL, 5),
            "exact_decimal_utc_batched_v3": (TPCH_Q6_UTC_BATCHED_LABEL, 5),
        }
        if self.scale_factor == 10:
            # SF10 starts directly with the corrected exact-decimal UTC
            # protocol; rejected SF1 development variants are not repeated.
            protocol_shape = {
                "exact_decimal_utc_batched_v3": (TPCH_Q6_SF10_FORMAL_LABEL, 5),
                "exact_decimal_utc_pollution_safe_paired_ci_v4": (
                    TPCH_Q6_SF10_PAIRED_CI_LABEL,
                    5,
                ),
            }
        elif self.scale_factor != 1:
            raise ValueError("TPC-H Q6 scale factor is not reviewed")
        expected = protocol_shape.get(self.timing_protocol)
        if expected is None or self.scientific_label != expected[0]:
            raise ValueError("TPC-H Q6 timing protocol and scientific label differ")
        if self.timing_protocol == "single_observation_v1":
            if self.timed_repeats_per_position != 1:
                raise ValueError("V1 uses exactly one timed observation per position")
        elif (
            self.timed_repeats_per_position < expected[1]
            or self.timed_repeats_per_position % 2 == 0
        ):
            raise ValueError("V2 requires an odd batch of at least five timed observations")
        if self.warmup_blocks < 6 or self.warmup_blocks % 6:
            raise ValueError("Q6 warmup must cover all six candidate permutations")
        if self.measured_blocks < 30 or self.measured_blocks % 6:
            raise ValueError("Q6 timing needs at least 30 blocks covering all permutations")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("TPC-H Q6 DuckDB controls are invalid")
        if self.duckdb_timezone != "UTC":
            raise ValueError("TPC-H Q6 execution timezone is frozen to UTC")
        for relative_path in (self.semantic_smoke_path, self.support_audit_path):
            path = Path(relative_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("TPC-H Q6 binding paths must stay inside the project")
        limits = (
            self.absolute_half_drift_limit,
            self.paired_ratio_half_drift_limit,
            self.paired_ratio_outlier_fraction_limit,
            self.tie_threshold_fraction,
        )
        if any(not 0.0 <= value < 1.0 for value in limits):
            raise ValueError("TPC-H Q6 stability limits must be in [0, 1)")
        if self.timing_protocol.endswith("pollution_safe_paired_ci_v4"):
            expected_carryover = {
                "materialize-after-q06-time",
                "materialize-after-q06-predicate",
            }
            if set(self.carryover_candidate_ids) != expected_carryover:
                raise ValueError("Q6 V4 must predeclare both materialized carryover routes")
            if self.measured_blocks < 60:
                raise ValueError("Q6 V4 needs 60 blocks for pollution-safe claims")
            if not 0.0 < self.carryover_tolerance_fraction < 1.0:
                raise ValueError("Q6 V4 carryover tolerance must be in (0, 1)")
            if not 0.0 < self.confidence_level < 1.0:
                raise ValueError("Q6 V4 confidence level must be in (0, 1)")
            if self.bootstrap_repetitions < 1000:
                raise ValueError("Q6 V4 requires at least 1000 bootstrap repetitions")
            if self.minimum_carryover_pairs < 5 or self.minimum_claim_blocks < 10:
                raise ValueError("Q6 V4 inferential sample minima are too small")
        for digest in (
            self.artifact_sha256,
            self.semantic_smoke_sha256,
            self.support_audit_sha256,
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("TPC-H Q6 bindings must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class TpchQ6Timing:
    block_index: int
    block_id: str
    permutation_id: str
    order_position: int
    inner_repeat_index: int
    candidate_id: str
    started_at_utc: str
    client_materialization_latency_ms: float
    process_cpu_time_ms: float
    output_row_count: int
    result_digest: str


def load_tpch_q6_formal_config(path: Path | str) -> TpchQ6FormalConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("TPC-H Q6 formal config must contain an object")
    return TpchQ6FormalConfig(
        results_dir=str(payload["results_dir"]),
        warmup_blocks=int(payload["warmup_blocks"]),
        measured_blocks=int(payload["measured_blocks"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        absolute_half_drift_limit=float(payload["absolute_half_drift_limit"]),
        paired_ratio_half_drift_limit=float(payload["paired_ratio_half_drift_limit"]),
        paired_ratio_outlier_fraction_limit=float(payload["paired_ratio_outlier_fraction_limit"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        artifact_sha256=str(payload["artifact_sha256"]),
        semantic_smoke_sha256=str(payload["semantic_smoke_sha256"]),
        support_audit_sha256=str(payload["support_audit_sha256"]),
        scale_factor=int(payload.get("scale_factor", 1)),
        require_clean_git=bool(payload["require_clean_git"]),
        scientific_label=str(payload["scientific_label"]),
        paper_performance_evidence=bool(payload["paper_performance_evidence"]),
        heldout_optimizer_evidence=bool(payload["heldout_optimizer_evidence"]),
        timing_protocol=str(payload.get("timing_protocol", "single_observation_v1")),
        timed_repeats_per_position=int(payload.get("timed_repeats_per_position", 1)),
        duckdb_timezone=str(payload.get("duckdb_timezone", "UTC")),
        semantic_smoke_path=str(
            payload.get("semantic_smoke_path", "results/tpch_q6_semantic_smoke/result.json")
        ),
        support_audit_path=str(
            payload.get("support_audit_path", "results/tpch_sf1_support_audit/audit.json")
        ),
        carryover_candidate_ids=tuple(payload.get("carryover_candidate_ids", ())),
        carryover_tolerance_fraction=float(payload.get("carryover_tolerance_fraction", 0.1)),
        confidence_level=float(payload.get("confidence_level", 0.95)),
        bootstrap_repetitions=int(payload.get("bootstrap_repetitions", 10000)),
        bootstrap_seed=int(payload.get("bootstrap_seed", 20260726)),
        minimum_carryover_pairs=int(payload.get("minimum_carryover_pairs", 5)),
        minimum_claim_blocks=int(payload.get("minimum_claim_blocks", 10)),
    )


def _environment(config: TpchQ6FormalConfig, commit: str) -> dict[str, Any]:
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
        "git_dirty": False,
        "packages": packages,
        "duckdb_threads": config.duckdb_threads,
        "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
        "gpu_acceleration": False,
        "cache_protocol": "hot_same_duckdb_connection",
        "duckdb_timezone": config.duckdb_timezone,
        "semantic_smoke_path": config.semantic_smoke_path,
        "support_audit_path": config.support_audit_path,
        "scale_factor": config.scale_factor,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _write_measurements(path: Path, rows: list[TpchQ6Timing]) -> None:
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TpchQ6Timing.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    os.replace(temporary, path)


def _read_measurements(path: Path) -> list[TpchQ6Timing]:
    """Reload atomically committed Q6 timing blocks for a safe resume."""

    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        records = list(csv.DictReader(handle))
    return [
        TpchQ6Timing(
            block_index=int(row["block_index"]),
            block_id=row["block_id"],
            permutation_id=row["permutation_id"],
            order_position=int(row["order_position"]),
            inner_repeat_index=int(row["inner_repeat_index"]),
            candidate_id=row["candidate_id"],
            started_at_utc=row["started_at_utc"],
            client_materialization_latency_ms=float(row["client_materialization_latency_ms"]),
            process_cpu_time_ms=float(row["process_cpu_time_ms"]),
            output_row_count=int(row["output_row_count"]),
            result_digest=row["result_digest"],
        )
        for row in records
    ]


def _completed_measurement_blocks(rows: list[TpchQ6Timing], config: TpchQ6FormalConfig) -> set[int]:
    """Reject partial persisted blocks instead of silently resuming from them."""

    grouped: dict[int, list[TpchQ6Timing]] = defaultdict(list)
    for row in rows:
        grouped[row.block_index].append(row)
    expected_rows = 3 * config.timed_repeats_per_position
    completed: set[int] = set()
    for block_index, block_rows in grouped.items():
        candidate_counts = Counter(row.candidate_id for row in block_rows)
        repeat_indices = {
            candidate_id: {
                item.inner_repeat_index for item in block_rows if item.candidate_id == candidate_id
            }
            for candidate_id in candidate_counts
        }
        complete = (
            0 <= block_index < config.measured_blocks
            and len(block_rows) == expected_rows
            and len(candidate_counts) == 3
            and set(candidate_counts.values()) == {config.timed_repeats_per_position}
            and all(
                values == set(range(config.timed_repeats_per_position))
                for values in repeat_indices.values()
            )
        )
        if not complete:
            raise GovernedRealDataSmokeError(
                f"TPC-H Q6 resume found an incomplete persisted block: {block_index}"
            )
        completed.add(block_index)
    return completed


def _stage_statistics(connection: Any) -> dict[str, int | float]:
    row = connection.execute(
        """
        SELECT
          COUNT(*) AS input_rows,
          COUNT(*) FILTER (
            WHERE l_shipdate >= DATE '1994-01-01'
              AND l_shipdate < DATE '1995-01-01'
          ) AS temporal_rows,
          COUNT(*) FILTER (
            WHERE l_shipdate >= DATE '1994-01-01'
              AND l_shipdate < DATE '1995-01-01'
              AND l_discount >= 0.05 AND l_discount <= 0.07
              AND l_quantity < 24
          ) AS governed_rows
        FROM lineitem
        """
    ).fetchone()
    if row is None:
        raise GovernedRealDataSmokeError("TPC-H Q6 stage statistics are missing")
    input_rows, temporal_rows, governed_rows = map(int, row)
    return {
        "input_rows": input_rows,
        "temporal_rows": temporal_rows,
        "temporal_selectivity": temporal_rows / input_rows,
        "governed_rows": governed_rows,
        "governed_selectivity": governed_rows / input_rows,
        "post_temporal_predicate_selectivity": governed_rows / temporal_rows,
    }


def run_tpch_q6_formal(
    config: TpchQ6FormalConfig,
    *,
    project_root: Path,
    show_progress: bool = False,
    resume_run_id: str | None = None,
) -> Path:
    """Run the three Q6 routes under the immutable paired protocol."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for formal TPC-H Q6") from exc
    root = project_root.resolve()
    bindings_to_check = (
        (root / config.semantic_smoke_path, config.semantic_smoke_sha256),
        (root / config.support_audit_path, config.support_audit_sha256),
    )
    for path, expected in bindings_to_check:
        if not path.is_file() or sha256_file(path) != expected:
            raise GovernedRealDataSmokeError(f"Frozen TPC-H Q6 binding changed: {path}")
    database, artifact = verify_tpch_artifact(root, scale_factor=config.scale_factor)
    if str(artifact["sha256"]) != config.artifact_sha256:
        raise GovernedRealDataSmokeError(f"Frozen TPC-H SF{config.scale_factor} artifact changed")
    if audit_source_freeze(root).status != "READY":
        raise GovernedRealDataSmokeError("formal TPC-H Q6 requires source READY")
    commit, dirty = tpch_git_state(root)
    if dirty:
        raise GovernedRealDataSmokeError("formal TPC-H Q6 requires a clean worktree")

    if resume_run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = root / config.results_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(run_dir / "config.json", asdict(config))
        run_environment = {
            **_environment(config, commit),
            "execution_segment_count": 1,
            "resumed_run": False,
        }
        _atomic_json(run_dir / "environment.json", run_environment)
        _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})
        timings: list[TpchQ6Timing] = []
    else:
        run_id = resume_run_id
        run_dir = root / config.results_dir / run_id
        if not run_dir.is_dir():
            raise GovernedRealDataSmokeError(f"TPC-H Q6 resume run is missing: {run_id}")
        stored_config = _load_json(run_dir / "config.json")
        run_environment = _load_json(run_dir / "environment.json")
        if stored_config != asdict(config):
            raise GovernedRealDataSmokeError("TPC-H Q6 resume config differs")
        if (
            run_environment.get("commit_hash") != commit
            or run_environment.get("git_dirty") is not False
        ):
            raise GovernedRealDataSmokeError("TPC-H Q6 resume source commit differs")
        timings = _read_measurements(run_dir / "measurements.csv")
        run_environment["execution_segment_count"] = (
            int(run_environment.get("execution_segment_count", 1)) + 1
        )
        run_environment["resumed_run"] = True
        run_environment["cache_protocol"] = "hot_per_process_segment"
        _atomic_json(run_dir / "environment.json", run_environment)
    completed_blocks = _completed_measurement_blocks(timings, config)

    examples = root / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    response = validate(_load_json(examples / "plans/q06.json"), policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("frozen TPC-H Q6 no longer validates")
    logical: ValidatedLogicalPlan = response.validated_plan
    candidates = generate_duckdb_candidates(
        logical, materialization_targets=TPCH_Q6_MATERIALIZATION_TARGETS
    )
    if len(candidates) != 3:
        raise GovernedRealDataSmokeError("TPC-H Q6 candidate space changed")

    extension = (root / "data/processed/duckdb_extensions").resolve()
    connection = duckdb.connect(str(database), read_only=True)
    compiled: dict[str, CompiledQuery] = {}
    plans: dict[str, dict[str, Any]] = {}
    fingerprints: set[str] = set()
    expected_digest: str | None = None
    # A resumed process must establish its own standardized hot-cache state.
    warmup_count = config.warmup_blocks
    progress = _Progress(
        3
        + 3 * warmup_count
        + 3 * (config.measured_blocks - len(completed_blocks)) * config.timed_repeats_per_position,
        show_progress,
    )
    try:
        connection.execute(f"SET TimeZone = {_sql_literal(config.duckdb_timezone)}")
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb-tpch-q6-formal"
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(temp_dir)}")
        connection.execute(f"SET extension_directory = {_sql_literal(extension)}")
        connection.execute("LOAD tpch")
        official_sql_row = connection.execute(
            "SELECT query FROM tpch_queries() WHERE query_nr = 6"
        ).fetchone()
        if official_sql_row is None:
            raise GovernedRealDataSmokeError("official TPC-H Q6 SQL is unavailable")
        official_row = connection.execute(str(official_sql_row[0])).fetchone()
        if official_row is None or len(official_row) != 1:
            raise GovernedRealDataSmokeError("official TPC-H Q6 did not return one scalar")
        official_revenue = official_row[0]
        table_bindings = create_tpch_q6_binding(connection)
        stage = _stage_statistics(connection)
        for candidate in candidates:
            candidate_id = candidate.strategy.strategy_id
            query = compile_approved_physical_plan(logical, candidate, catalog, table_bindings)
            execution = execute_with_connection(query, connection)
            if execution.rows != ((official_revenue,),):
                raise GovernedRealDataSmokeError(
                    f"TPC-H Q6 preflight differs from official result for {candidate_id}"
                )
            if expected_digest is None:
                expected_digest = execution.result_digest
            elif execution.result_digest != expected_digest:
                raise GovernedRealDataSmokeError("TPC-H Q6 candidate outputs differ")
            certificate = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"tpch-q6-formal-{run_id}-{candidate_id}",
            )
            observation = observe_duckdb_plan(connection, query.sql, query.parameters, analyze=True)
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError("TPC-H Q6 physical plans collapsed")
            fingerprints.add(observation.fingerprint)
            compiled[candidate_id] = query
            plans[candidate_id] = {
                "physical_plan_id": candidate.physical_plan_id,
                "duckdb_plan_fingerprint": observation.fingerprint,
                "duckdb_operator_names": list(observation.operator_names),
                "actual_cardinalities": list(observation.actual_cardinalities),
                "rows_scanned": list(observation.rows_scanned),
                "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                "certificate_status": certificate,
            }
            progress.advance(f"preflight {candidate_id}")
        candidate_ids = tuple(compiled)
        warmup_orders = complete_permutation_orders(
            candidate_ids, warmup_count, seed=config.order_seed
        )
        measured_orders = complete_permutation_orders(
            candidate_ids, config.measured_blocks, seed=config.order_seed + 1
        )
        schedule = [(False, i, order) for i, order in enumerate(warmup_orders)] + [
            (True, i, order) for i, order in enumerate(measured_orders) if i not in completed_blocks
        ]
        for measured, block_index, order in schedule:
            block_id = f"tpch-q6-block-{block_index:03d}"
            permutation_id = " -> ".join(order)
            for position, candidate_id in enumerate(order):
                repeat_count = config.timed_repeats_per_position if measured else 1
                for inner_repeat in range(repeat_count):
                    started_at = datetime.now(UTC).isoformat()
                    cpu_started = time.process_time_ns()
                    started = time.perf_counter_ns()
                    execution = execute_with_connection(compiled[candidate_id], connection)
                    latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
                    if execution.result_digest != expected_digest:
                        raise GovernedRealDataSmokeError("TPC-H Q6 timed result changed")
                    if measured:
                        timings.append(
                            TpchQ6Timing(
                                block_index,
                                block_id,
                                permutation_id,
                                position,
                                inner_repeat,
                                candidate_id,
                                started_at,
                                latency_ms,
                                cpu_ms,
                                execution.row_count,
                                execution.result_digest,
                            )
                        )
                    progress.advance(
                        f"{'measure' if measured else 'warmup'} {candidate_id} "
                        f"repeat {inner_repeat + 1}/{repeat_count}"
                    )
            if measured:
                _write_measurements(run_dir / "measurements.csv", timings)
            _atomic_json(
                run_dir / "progress.json",
                {
                    "completed_blocks": block_index + 1,
                    "phase": "measured" if measured else "warmup",
                    "updated_at_utc": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        connection.close()

    expected_measurements = 3 * config.measured_blocks * config.timed_repeats_per_position
    if len(timings) != expected_measurements:
        raise GovernedRealDataSmokeError(
            f"TPC-H Q6 expected {expected_measurements} measurements, found {len(timings)}"
        )
    by_candidate = {
        candidate_id: [
            row.client_materialization_latency_ms
            for row in timings
            if row.candidate_id == candidate_id
        ]
        for candidate_id in compiled
    }
    summaries = {
        candidate_id: {
            "runs": len(values),
            "median_ms": statistics.median(values),
            "p95_ms": _percentile(values, 0.95),
            "min_ms": min(values),
            "max_ms": max(values),
            **plans[candidate_id],
        }
        for candidate_id, values in by_candidate.items()
    }
    _write_measurements(run_dir / "measurements.csv", timings)
    _atomic_json(
        run_dir / "summary.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": "PASS",
            "scientific_label": config.scientific_label,
            "paper_performance_evidence": True,
            "heldout_optimizer_evidence": False,
            "optimizer_selection_evaluated": False,
            "cache_protocol": run_environment["cache_protocol"],
            "execution_segment_count": run_environment["execution_segment_count"],
            "resumed_run": run_environment["resumed_run"],
            "timing_protocol": config.timing_protocol,
            "timed_repeats_per_position": config.timed_repeats_per_position,
            "paired_block_statistic": (
                "single_observation"
                if config.timed_repeats_per_position == 1
                else f"median_of_{config.timed_repeats_per_position}"
            ),
            "artifact_sha256": config.artifact_sha256,
            "scale_factor": config.scale_factor,
            "official_revenue_decimal": str(official_revenue),
            "official_result_equivalent_preflight": True,
            "stage_statistics": stage,
            "candidate_count": len(compiled),
            "distinct_duckdb_plan_count": len(fingerprints),
            "candidate_summaries": summaries,
            "measurement_count": len(timings),
        },
    )
    return run_dir


def _half_drift(values: list[float]) -> float:
    midpoint = len(values) // 2
    first = statistics.median(values[:midpoint])
    second = statistics.median(values[midpoint:])
    return abs(second / first - 1.0)


def _outlier_fraction(values: list[float]) -> float:
    center = statistics.median(values)
    deviations = [abs(value - center) for value in values]
    threshold = max(3.0 * statistics.median(deviations), 0.15)
    return sum(value > threshold for value in deviations) / len(values)


def analyze_tpch_q6_formal(run_dir: Path) -> dict[str, Any]:
    """Apply the frozen balance and paired-stability gates."""

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    candidate_ids = tuple(summary["candidate_summaries"])
    by_candidate_block: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_block_raw: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    permutation_by_block: dict[int, str] = {}
    for row in rows:
        candidate_id = row["candidate_id"]
        block = int(row["block_index"])
        latency = float(row["client_materialization_latency_ms"])
        by_candidate_block[candidate_id][block].append(latency)
        by_block_raw[block][candidate_id].append(latency)
        permutation_by_block[block] = row["permutation_id"]
    by_block = {
        block: {
            candidate_id: statistics.median(values) for candidate_id, values in candidates.items()
        }
        for block, candidates in by_block_raw.items()
    }
    ratios = {
        candidate_id: [
            values[candidate_id] / values["fused"] for _, values in sorted(by_block.items())
        ]
        for candidate_id in candidate_ids
        if candidate_id != "fused"
    }
    absolute_drift = {
        candidate_id: _half_drift(
            [statistics.median(values) for _, values in sorted(blocks.items())]
        )
        for candidate_id, blocks in by_candidate_block.items()
    }
    ratio_drift = {candidate_id: _half_drift(values) for candidate_id, values in ratios.items()}
    outliers = {candidate_id: _outlier_fraction(values) for candidate_id, values in ratios.items()}
    positions: dict[str, Counter[int]] = defaultdict(Counter)
    for block, candidates in by_block_raw.items():
        first_row_for_block = [row for row in rows if int(row["block_index"]) == block]
        for candidate_id in candidates:
            position = next(
                int(row["order_position"])
                for row in first_row_for_block
                if row["candidate_id"] == candidate_id
            )
            positions[candidate_id][position] += 1
    permutation_counts = Counter(permutation_by_block.values())
    integrity = {
        "clean_source_recorded": environment.get("git_dirty") is False,
        "single_execution_process": int(environment.get("execution_segment_count", 1)) == 1,
        "candidate_space_complete": len(candidate_ids) == 3
        and int(summary["distinct_duckdb_plan_count"]) == 3,
        "measurements_complete": len(rows)
        == 3 * int(config["measured_blocks"]) * int(config.get("timed_repeats_per_position", 1)),
        "all_6_permutations_balanced": len(permutation_counts) == 6
        and set(permutation_counts.values()) == {int(config["measured_blocks"]) // 6},
        "all_positions_balanced": all(
            set(counts) == {0, 1, 2} and len(set(counts.values())) == 1
            for counts in positions.values()
        ),
        "certificates_partial": all(
            item["certificate_status"] == "PARTIAL"
            for item in summary["candidate_summaries"].values()
        ),
        "resources_observed": all(
            int(item["peak_buffer_memory_bytes"]) > 0
            and int(item["peak_temp_directory_bytes"]) >= 0
            for item in summary["candidate_summaries"].values()
        ),
    }
    stability = {
        "absolute_half_drift": max(absolute_drift.values())
        <= float(config["absolute_half_drift_limit"]),
        "paired_ratio_half_drift": max(ratio_drift.values())
        <= float(config["paired_ratio_half_drift_limit"]),
        "paired_ratio_outlier_fraction": max(outliers.values())
        <= float(config["paired_ratio_outlier_fraction_limit"]),
    }
    normalized = {"fused": 1.0, **{key: statistics.median(value) for key, value in ratios.items()}}
    best = min(normalized.values())
    tie = float(config["tie_threshold_fraction"])
    oracle_set = sorted(
        candidate_id for candidate_id, ratio in normalized.items() if ratio <= best * (1.0 + tie)
    )
    inference_v2 = str(config["timing_protocol"]).endswith("pollution_safe_paired_ci_v4")
    carryover_assessments: list[dict[str, Any]] = []
    paired_claims: list[dict[str, Any]] = []
    inference_ready = True
    if inference_v2:
        carryover_ids = tuple(str(item) for item in config["carryover_candidate_ids"])
        carryover_assessments = assess_carryover(
            rows,
            candidate_ids=candidate_ids,
            carryover_candidate_ids=carryover_ids,
            tolerance_fraction=float(config["carryover_tolerance_fraction"]),
            confidence_level=float(config["confidence_level"]),
            bootstrap_repetitions=int(config["bootstrap_repetitions"]),
            bootstrap_seed=int(config["bootstrap_seed"]),
            minimum_pairs=int(config["minimum_carryover_pairs"]),
        )
        paired_claims = authorize_paired_claims(
            rows,
            candidate_ids=candidate_ids,
            baseline_id="fused",
            carryover_candidate_ids=carryover_ids,
            tie_fraction=float(config["tie_threshold_fraction"]),
            confidence_level=float(config["confidence_level"]),
            bootstrap_repetitions=int(config["bootstrap_repetitions"]),
            bootstrap_seed=int(config["bootstrap_seed"]),
            minimum_blocks=int(config["minimum_claim_blocks"]),
        )
        inference_ready = all(
            item["classification"] != "INSUFFICIENT_PAIRS" for item in carryover_assessments
        ) and all(item["conclusion"] != "INSUFFICIENT_BLOCKS" for item in paired_claims)
    passed = all(integrity.values()) and inference_ready
    if not inference_v2:
        passed = passed and all(stability.values())
    authorized_claim_count = sum(item["claim_authorized"] for item in paired_claims)
    paper_authorized = passed and (not inference_v2 or authorized_claim_count > 0)
    payload = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "status": "PASS" if passed else "FAIL",
        "scientific_label": summary["scientific_label"],
        "formal_paper_experiment_authorized": paper_authorized,
        "paper_performance_evidence_requested": True,
        "paper_performance_evidence": paper_authorized,
        "heldout_optimizer_evidence": False,
        "optimizer_selection_evaluated": False,
        "timing_protocol": summary["timing_protocol"],
        "paired_block_statistic": summary["paired_block_statistic"],
        "integrity_gates": integrity,
        "stability_gates": stability,
        "absolute_half_drift_by_candidate": absolute_drift,
        "paired_ratio_half_drift_by_candidate": ratio_drift,
        "paired_ratio_outlier_fraction_by_candidate": outliers,
        "legacy_stability_diagnostics": stability if inference_v2 else None,
        "carryover_assessments": carryover_assessments,
        "paired_claims": paired_claims,
        "claim_authorization_required": inference_v2,
        "authorized_claim_count": authorized_claim_count,
        "inference_protocol_ready": inference_ready,
        "median_candidate_over_fused_ratio": normalized,
        "diagnostic_oracle_set_within_tie_band": oracle_set,
        "scientific_boundary": (
            f"TPC-H SF{config.get('scale_factor', 1)} Q6 is standard-benchmark method "
            "evidence. V4 authorizes only claims whose predeclared pollution-safe paired "
            "confidence interval is conclusive; an Oracle point estimate never authorizes a "
            "claim. A resumed multi-process run is diagnostic only."
            if inference_v2
            else f"TPC-H SF{config.get('scale_factor', 1)} Q6 is standard-benchmark method "
            "evidence. The Oracle is computed after running all candidates and is not "
            "optimizer selection or heldout evidence. A resumed multi-process run is "
            "retained as diagnostic evidence only."
        ),
    }
    _atomic_json(run_dir / "acceptance.json", payload)
    lines = [
        f"# Governed TPC-H SF{config.get('scale_factor', 1)} Q6 paired measurement",
        "",
        f"Status: **{payload['status']}**",
        "",
        "| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for candidate_id, values in summary["candidate_summaries"].items():
        lines.append(
            f"| {candidate_id} | {values['median_ms']:.3f} | {values['p95_ms']:.3f} | "
            f"{values['peak_buffer_memory_bytes'] / 1048576:.2f} |"
        )
    lines.extend(
        [
            "",
            f"- Six-permutation balance: `{integrity['all_6_permutations_balanced']}`",
            f"- Single execution process: `{integrity['single_execution_process']}`",
            f"- Stability gates: `{stability}`",
            f"- Paired 3% diagnostic Oracle set: `{oracle_set}`",
            "- Optimizer selection evaluated: `False`.",
            "",
        ]
    )
    if inference_v2:
        lines.extend(
            [
                "## Carryover checks",
                "",
                "| Possible polluter | Target | Exposed/control | 95% CI | Assessment |",
                "| --- | --- | ---: | --- | --- |",
            ]
        )
        for item in carryover_assessments:
            interval = item["confidence_interval"]
            lines.append(
                f"| {item['carryover_candidate_id']} | {item['target_candidate_id']} | "
                f"{item['median_exposed_over_control_ratio']:.4f} | "
                f"[{interval['lower']:.4f}, {interval['upper']:.4f}] | "
                f"{item['classification']} |"
            )
        lines.extend(
            [
                "",
                "## Pollution-safe paired claims",
                "",
                "| Candidate | Baseline | Paired blocks | Ratio | 95% CI | "
                "Conclusion | Authorized |",
                "| --- | --- | ---: | ---: | --- | --- | --- |",
            ]
        )
        for item in paired_claims:
            interval = item["confidence_interval"]
            lines.append(
                f"| {item['candidate_id']} | {item['baseline_id']} | "
                f"{item['pollution_safe_block_count']} | "
                f"{item['median_candidate_over_baseline_ratio']:.4f} | "
                f"[{interval['lower']:.4f}, {interval['upper']:.4f}] | "
                f"{item['conclusion']} | {item['claim_authorized']} |"
            )
        lines.append("")
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return payload
