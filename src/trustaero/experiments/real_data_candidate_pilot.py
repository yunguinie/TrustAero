"""Balanced, resumable performance pilot for approved real-data candidates."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import os
import platform
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import (
    verify_real_data_full_month_artifacts,
    verify_real_data_slice_artifacts,
)
from trustaero.data.download import sha256_file
from trustaero.execution import (
    CompiledQuery,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.phase2c import balanced_candidate_orders
from trustaero.experiments.real_data_candidates import (
    _TARGETS,
    _candidate_exposure,
    _raw_plan,
    verify_candidate_execution_certificate,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _create_full_month_views,
    _create_trusted_views,
    _load_json,
)
from trustaero.experiments.real_data_pilot import (
    _git_state,
    _Progress,
    _semantic_digest,
    _stage_statistics,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.optimizer.candidate_feasibility import (
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility import audit_source_freeze
from trustaero.validator.service import validate

PILOT_LABEL = "real_data_multi_candidate_pilot_not_paper_evidence"
FORMAL_CANDIDATE_LABEL = "real_data_formal_development_partition_v1"


@dataclass(frozen=True, slots=True)
class RealDataCandidatePilotConfig:
    results_dir: str
    workloads: tuple[str, ...]
    sample_rows: tuple[int, ...]
    warmup_runs: int = 1
    measured_runs: int = 5
    duckdb_threads: int = 4
    duckdb_memory_limit_mb: int = 4096
    order_seed: int = 20260719
    require_clean_git: bool = False
    full_month: bool = False
    order_protocol: Literal["cyclic", "all_permutations"] = "cyclic"
    absolute_half_drift_limit: float = 0.50
    paired_ratio_half_drift_limit: float = 0.20
    paired_ratio_outlier_fraction_limit: float = 0.10
    scientific_label: str = PILOT_LABEL
    paper_performance_evidence: bool = False
    heldout_optimizer_evidence: bool = False
    query_family_protocol_sha256: str | None = None
    semantic_smoke_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.workloads or set(self.workloads) - set(_TARGETS):
            raise ValueError("candidate pilot contains unsupported workloads")
        if (not self.full_month and not self.sample_rows) or any(
            value < 1 for value in self.sample_rows
        ):
            raise ValueError("candidate pilot sample rows must be positive")
        if self.full_month and self.sample_rows:
            raise ValueError("full-month pilot derives row counts from frozen artifacts")
        if len(set(self.workloads)) != len(self.workloads) or len(set(self.sample_rows)) != len(
            self.sample_rows
        ):
            raise ValueError("candidate pilot dimensions cannot contain duplicates")
        if self.warmup_runs < 0 or self.measured_runs < 1:
            raise ValueError("candidate pilot run counts are invalid")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("candidate pilot DuckDB controls are invalid")
        if self.paper_performance_evidence:
            if (
                self.scientific_label != FORMAL_CANDIDATE_LABEL
                or not self.require_clean_git
                or not self.full_month
                or self.order_protocol != "all_permutations"
                or self.measured_runs < 30
            ):
                raise ValueError("formal candidate timing controls are incomplete")
            for digest in (
                self.query_family_protocol_sha256,
                self.semantic_smoke_sha256,
            ):
                if digest is None or len(digest) != 64:
                    raise ValueError("formal candidate timing requires SHA-256 bindings")
        elif self.scientific_label != PILOT_LABEL:
            raise ValueError("candidate pilot scientific boundary cannot be weakened")
        if self.heldout_optimizer_evidence:
            raise ValueError("the January development partition is not optimizer holdout evidence")
        if self.order_protocol == "all_permutations" and self.measured_runs % 6:
            raise ValueError("all-permutations measured_runs must be divisible by six")
        for value in (
            self.absolute_half_drift_limit,
            self.paired_ratio_half_drift_limit,
            self.paired_ratio_outlier_fraction_limit,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("candidate pilot stability limits must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class CandidateTiming:
    unit_id: str
    workload: str
    sample_rows: int
    repeat_index: int
    measurement_block_id: str
    permutation_id: str
    order_position: int
    strategy_id: str
    started_at_utc: str
    client_materialization_latency_ms: float
    process_cpu_time_ms: float
    output_row_count: int
    semantic_result_digest: str


def load_candidate_pilot_config(path: Path | str) -> RealDataCandidatePilotConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate pilot config must be an object")
    return RealDataCandidatePilotConfig(
        results_dir=str(payload["results_dir"]),
        workloads=tuple(str(item) for item in payload["workloads"]),
        sample_rows=tuple(int(item) for item in payload["sample_rows"]),
        warmup_runs=int(payload.get("warmup_runs", 1)),
        measured_runs=int(payload.get("measured_runs", 5)),
        duckdb_threads=int(payload.get("duckdb_threads", 4)),
        duckdb_memory_limit_mb=int(payload.get("duckdb_memory_limit_mb", 4096)),
        order_seed=int(payload.get("order_seed", 20260719)),
        require_clean_git=bool(payload.get("require_clean_git", False)),
        full_month=bool(payload.get("full_month", False)),
        order_protocol=str(payload.get("order_protocol", "cyclic")),  # type: ignore[arg-type]
        absolute_half_drift_limit=float(payload.get("absolute_half_drift_limit", 0.50)),
        paired_ratio_half_drift_limit=float(payload.get("paired_ratio_half_drift_limit", 0.20)),
        paired_ratio_outlier_fraction_limit=float(
            payload.get("paired_ratio_outlier_fraction_limit", 0.10)
        ),
        scientific_label=str(payload.get("scientific_label", "")),
        paper_performance_evidence=bool(payload.get("paper_performance_evidence", False)),
        heldout_optimizer_evidence=bool(payload.get("heldout_optimizer_evidence", False)),
        query_family_protocol_sha256=(
            str(payload["query_family_protocol_sha256"])
            if payload.get("query_family_protocol_sha256") is not None
            else None
        ),
        semantic_smoke_sha256=(
            str(payload["semantic_smoke_sha256"])
            if payload.get("semantic_smoke_sha256") is not None
            else None
        ),
    )


def _environment(config: RealDataCandidatePilotConfig, commit: str, dirty: bool) -> dict[str, Any]:
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
        "duckdb_threads": config.duckdb_threads,
        "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
        "gpu_acceleration": False,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _stable_offset(unit_id: str, seed: int) -> int:
    value = hashlib.sha256(f"{unit_id}:{seed}".encode()).digest()
    return int.from_bytes(value[:8], "big")


def complete_permutation_orders(
    strategy_ids: tuple[str, ...],
    round_count: int,
    *,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    """Cover every candidate permutation equally in deterministic shuffled blocks."""

    permutations = list(itertools.permutations(strategy_ids))
    if not permutations or round_count < 0:
        raise ValueError("permutation schedule requires candidates and nonnegative rounds")
    orders: list[tuple[str, ...]] = []
    block_index = 0
    while len(orders) < round_count:
        block = list(permutations)
        random.Random(seed + block_index).shuffle(block)
        orders.extend(block)
        block_index += 1
    return tuple(orders[:round_count])


def _profiles() -> tuple[GovernanceFeasibilityPolicy, ...]:
    return (
        GovernanceFeasibilityPolicy("output-mask-only", None, None),
        GovernanceFeasibilityPolicy("no-raw-sensitive-materialization", None, 0),
    )


def _run_unit(
    *,
    root: Path,
    config: RealDataCandidatePilotConfig,
    workload: str,
    sample_rows: int,
    progress: _Progress,
    full_month: bool = False,
) -> dict[str, Any]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for candidate pilot") from exc

    unit_id = f"{workload}-full-2024-01" if full_month else f"{workload}-n{sample_rows}"
    artifacts = (
        verify_real_data_full_month_artifacts(root / "data", workload)
        if full_month
        else verify_real_data_slice_artifacts(root / "data", sample_rows)
    )
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    response = validate(_raw_plan(examples, workload), policy, catalog)
    if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
        raise GovernedRealDataSmokeError(f"{unit_id} logical plan was not approved")
    logical: ValidatedLogicalPlan | None = response.validated_plan
    if logical is None:
        raise GovernedRealDataSmokeError(f"{unit_id} has no validated plan")

    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        spill = root / "data/tmp/duckdb" / f"candidate-{unit_id}"
        spill.mkdir(parents=True, exist_ok=True)
        escaped_spill = str(spill).replace("'", "''")
        connection.execute(f"SET temp_directory = '{escaped_spill}'")
        bindings = (
            _create_full_month_views(connection, root / "data", workload=workload)
            if full_month
            else _create_trusted_views(connection, root / "data", sample_rows=sample_rows)
        )
        stage = _stage_statistics(connection, workload)
        candidates = generate_duckdb_candidates(
            logical,
            materialization_targets=_TARGETS[workload],
        )
        exposures = tuple(
            _candidate_exposure(
                workload=workload,
                strategy_id=item.strategy.strategy_id,
                materialize_after=item.strategy.materialize_after,
                governed_rows=int(stage["governed_rows"]),
            )
            for item in candidates
        )
        feasibility = {
            profile.policy_id: filter_feasible_candidates(exposures, profile)
            for profile in _profiles()
        }
        compiled: dict[str, CompiledQuery] = {}
        plans: dict[str, dict[str, Any]] = {}
        expected_digest: str | None = None
        fingerprints: set[str] = set()
        for candidate, exposure in zip(candidates, exposures, strict=True):
            strategy_id = candidate.strategy.strategy_id
            query = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(query, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError(f"{unit_id} candidate outputs differ")
            certificate_status = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"pilot-{unit_id}-{strategy_id}",
            )
            observation = observe_duckdb_plan(
                connection,
                query.sql,
                query.parameters,
                # This excluded preflight execution captures real cardinality,
                # memory, and spill. It is never mixed into latency samples.
                analyze=True,
            )
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError(f"{unit_id} physical candidates collapsed")
            fingerprints.add(observation.fingerprint)
            compiled[strategy_id] = query
            plans[strategy_id] = {
                "physical_plan_id": candidate.physical_plan_id,
                "duckdb_plan_fingerprint": observation.fingerprint,
                "duckdb_operator_names": list(observation.operator_names),
                "actual_cardinalities": list(observation.actual_cardinalities),
                "rows_scanned": list(observation.rows_scanned),
                "max_intermediate_cardinality": observation.max_intermediate_cardinality,
                "profile_latency_ms_single_observation": observation.profile_latency_ms,
                "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                "certificate_status": certificate_status,
                "exposure": asdict(exposure),
            }
            progress.advance(f"{unit_id} preflight {strategy_id}")

        strategy_ids = tuple(compiled)
        offset = _stable_offset(unit_id, config.order_seed)
        if config.order_protocol == "all_permutations":
            warmup_orders = complete_permutation_orders(
                strategy_ids,
                config.warmup_runs,
                seed=offset + 1,
            )
            measured_orders = complete_permutation_orders(
                strategy_ids,
                config.measured_runs,
                seed=offset + 2,
            )
        else:
            all_orders = balanced_candidate_orders(
                strategy_ids,
                config.warmup_runs + config.measured_runs,
                offset_seed=offset,
            )
            warmup_orders = all_orders[: config.warmup_runs]
            measured_orders = all_orders[config.warmup_runs :]
        timings: list[CandidateTiming] = []
        scheduled_orders = [(False, index, order) for index, order in enumerate(warmup_orders)] + [
            (True, index, order) for index, order in enumerate(measured_orders)
        ]
        for measured, repeat_index, order in scheduled_orders:
            permutation_id = " -> ".join(order)
            block_id = f"{unit_id}-block-{repeat_index:03d}"
            for position, strategy_id in enumerate(order):
                started_at_utc = datetime.now(UTC).isoformat()
                cpu_started = time.process_time_ns()
                started = time.perf_counter_ns()
                execution = execute_with_connection(compiled[strategy_id], connection)
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                process_cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
                digest = _semantic_digest(execution.columns, execution.rows)
                if digest != expected_digest:
                    raise GovernedRealDataSmokeError(f"{unit_id} timed output changed")
                if measured:
                    timings.append(
                        CandidateTiming(
                            unit_id=unit_id,
                            workload=workload,
                            sample_rows=sample_rows,
                            repeat_index=repeat_index,
                            measurement_block_id=block_id,
                            permutation_id=permutation_id,
                            order_position=position,
                            strategy_id=strategy_id,
                            started_at_utc=started_at_utc,
                            client_materialization_latency_ms=latency_ms,
                            process_cpu_time_ms=process_cpu_ms,
                            output_row_count=execution.row_count,
                            semantic_result_digest=digest,
                        )
                    )
                label = "measured" if measured else "warmup"
                progress.advance(f"{unit_id} {label} {strategy_id}")
    finally:
        connection.close()

    by_strategy: dict[str, list[float]] = {item: [] for item in compiled}
    for timing in timings:
        by_strategy[timing.strategy_id].append(timing.client_materialization_latency_ms)
    summaries = {
        strategy_id: {
            "median_ms": statistics.median(values),
            "p95_ms": _percentile(values, 0.95),
            "min_ms": min(values),
            "max_ms": max(values),
            **plans[strategy_id],
        }
        for strategy_id, values in by_strategy.items()
    }
    profile_results: dict[str, dict[str, Any]] = {}
    for profile_id, result in feasibility.items():
        feasible = result.feasible_candidate_ids
        oracle = min(feasible, key=lambda item: float(summaries[item]["median_ms"]))
        oracle_ms = float(summaries[oracle]["median_ms"])
        fused_ms = float(summaries["fused"]["median_ms"])
        profile_results[profile_id] = {
            "feasible_candidate_ids": list(feasible),
            "rejected_candidate_ids": list(result.rejected_candidate_ids),
            "oracle_strategy_id": oracle,
            "oracle_median_ms": oracle_ms,
            "fixed_fused_median_ms": fused_ms,
            "oracle_opportunity_speedup_vs_fused": fused_ms / oracle_ms,
            "optimizer_selection_evaluated": False,
        }
    return {
        "unit_id": unit_id,
        "status": "PASS",
        "scientific_label": config.scientific_label,
        "paper_performance_evidence": config.paper_performance_evidence,
        "heldout_optimizer_evidence": config.heldout_optimizer_evidence,
        "workload": workload,
        "sample_rows": sample_rows,
        "full_month": full_month,
        "verified_execution_artifacts": [asdict(item) for item in artifacts],
        "stage_statistics": stage,
        "candidate_count": len(compiled),
        "distinct_duckdb_plan_count": len(fingerprints),
        "candidate_summaries": summaries,
        "policy_profiles": profile_results,
        "measurements": [asdict(item) for item in timings],
    }


def _write_measurements(run_dir: Path, units: list[dict[str, Any]]) -> None:
    rows = [row for unit in units for row in unit["measurements"]]
    with (run_dir / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CandidateTiming.__annotations__))
        writer.writeheader()
        writer.writerows(rows)


def run_real_data_candidate_pilot(
    config: RealDataCandidatePilotConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or safely resume all workload/size units."""

    root = project_root.resolve()
    if config.paper_performance_evidence:
        bindings = (
            (
                root / "experiments/configs/real_data_query_families_v1.json",
                config.query_family_protocol_sha256,
            ),
            (
                root / "data/manifests/processed/real-data-candidate-smoke.json",
                config.semantic_smoke_sha256,
            ),
        )
        for path, expected in bindings:
            if expected is None or not path.is_file() or sha256_file(path) != expected:
                raise GovernedRealDataSmokeError(f"Formal candidate binding changed: {path}")
        freeze = audit_source_freeze(root)
        if freeze.status != "READY":
            raise GovernedRealDataSmokeError("formal candidate timing requires source READY")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("candidate pilot requires a clean worktree")
    results_root = root / config.results_dir
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = results_root / run_id
    units_dir = run_dir / "units"
    units_dir.mkdir(parents=True, exist_ok=True)
    config_payload = json.loads(json.dumps(asdict(config), sort_keys=True))
    config_path = run_dir / "config.json"
    if resume_run_id and config_path.is_file():
        if json.loads(config_path.read_text(encoding="utf-8")) != config_payload:
            raise GovernedRealDataSmokeError("resume config differs from frozen pilot")
    _atomic_json(config_path, config_payload)
    _atomic_json(run_dir / "environment.json", _environment(config, commit, dirty))
    if resume_run_id is None:
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})

    schedule = (
        [(workload, 547_271 if workload == "bts" else 2_964_624) for workload in config.workloads]
        if config.full_month
        else [(workload, rows) for workload in config.workloads for rows in config.sample_rows]
    )
    random.Random(config.order_seed).shuffle(schedule)
    completed = {path.stem for path in units_dir.glob("*.json")}
    pending = [
        item
        for item in schedule
        if (f"{item[0]}-full-2024-01" if config.full_month else f"{item[0]}-n{item[1]}")
        not in completed
    ]
    steps_per_unit = 3 * (1 + config.warmup_runs + config.measured_runs)
    progress = _Progress(len(pending) * steps_per_unit, show_progress)
    for workload, rows in pending:
        unit = _run_unit(
            root=root,
            config=config,
            workload=workload,
            sample_rows=rows,
            progress=progress,
            full_month=config.full_month,
        )
        _atomic_json(units_dir / f"{unit['unit_id']}.json", unit)
        completed.add(str(unit["unit_id"]))
        _atomic_json(
            run_dir / "progress.json",
            {
                "run_id": run_id,
                "completed_units": len(completed),
                "total_units": len(schedule),
                "last_completed_unit": unit["unit_id"],
                "updated_at_utc": datetime.now(UTC).isoformat(),
            },
        )
    units = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(units_dir.glob("*.json"))
    ]
    _write_measurements(run_dir, units)
    _atomic_json(
        run_dir / "summary.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": "PASS",
            "scientific_label": config.scientific_label,
            "paper_performance_evidence": config.paper_performance_evidence,
            "heldout_optimizer_evidence": config.heldout_optimizer_evidence,
            "optimizer_selection_evaluated": False,
            "completed_units": len(units),
            "expected_units": len(schedule),
            "units": [
                {key: value for key, value in unit.items() if key != "measurements"}
                for unit in units
            ],
        },
    )
    return run_dir
