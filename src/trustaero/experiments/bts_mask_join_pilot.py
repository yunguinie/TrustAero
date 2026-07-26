"""Paired timing-protocol validation for the frozen BTS Mask/Join query.

This runner is intentionally labelled as non-paper evidence.  It checks that a
balanced hot-cache protocol can measure the already validated early/late Mask
routes without changing query semantics or relaxing governance constraints.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import (
    verify_bts_mask_join_full_month_artifacts,
    verify_bts_mask_join_slice_artifacts,
)
from trustaero.data.download import sha256_file
from trustaero.execution import (
    CompiledQuery,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.bts_mask_join import (
    BTS_MASK_JOIN_TARGET,
    _create_bts_mask_join_views,
    _filtered_row_count,
)
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_candidates import (
    verify_candidate_execution_certificate,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _git_state, _Progress, _semantic_digest
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import Mask, PolicySet, ValidatedLogicalPlan
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility import audit_source_freeze
from trustaero.validator.service import validate

MASK_JOIN_PILOT_LABEL = "bts_mask_join_paired_protocol_validation_not_paper_evidence"
MASK_JOIN_FORMAL_LABEL = "bts_mask_join_formal_development_partition_v1"
LATE_CANDIDATE = "late_mask_fused"
EARLY_CANDIDATE = "early_mask_before_join"


@dataclass(frozen=True, slots=True)
class BtsMaskJoinPilotConfig:
    results_dir: str
    sample_rows: int
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    absolute_half_drift_limit: float
    paired_ratio_half_drift_limit: float
    paired_ratio_outlier_fraction_limit: float
    tie_threshold_fraction: float
    query_family_protocol_sha256: str
    semantic_smoke_sha256: str
    full_month: bool = False
    require_clean_git: bool = False
    scientific_label: str = MASK_JOIN_PILOT_LABEL
    paper_performance_evidence: bool = False
    heldout_optimizer_evidence: bool = False

    def __post_init__(self) -> None:
        if not self.results_dir or self.sample_rows < 1:
            raise ValueError("Mask/Join pilot path and sample size must be valid")
        if self.full_month and self.sample_rows != 547_271:
            raise ValueError("full-month BTS Mask/Join must bind the frozen 547271 rows")
        if self.warmup_blocks < 0 or self.warmup_blocks % 2:
            raise ValueError("warmup blocks must be nonnegative and cover both permutations")
        if self.measured_blocks < 2 or self.measured_blocks % 2:
            raise ValueError("measured blocks must be positive and cover both permutations")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("Mask/Join DuckDB controls are invalid")
        for value in (
            self.absolute_half_drift_limit,
            self.paired_ratio_half_drift_limit,
            self.paired_ratio_outlier_fraction_limit,
            self.tie_threshold_fraction,
        ):
            if not 0.0 <= value < 1.0:
                raise ValueError("Mask/Join stability limits must be in [0, 1)")
        for digest in (self.query_family_protocol_sha256, self.semantic_smoke_sha256):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("Mask/Join frozen bindings must be lowercase SHA-256 values")
        if self.paper_performance_evidence:
            if (
                self.scientific_label != MASK_JOIN_FORMAL_LABEL
                or not self.require_clean_git
                or not self.full_month
                or self.measured_blocks < 30
            ):
                raise ValueError("formal Mask/Join timing controls are incomplete")
        elif self.scientific_label != MASK_JOIN_PILOT_LABEL:
            raise ValueError("Mask/Join pilot scientific boundary cannot be weakened")
        if self.heldout_optimizer_evidence:
            raise ValueError("the January development partition is not optimizer holdout evidence")


@dataclass(frozen=True, slots=True)
class MaskJoinTiming:
    block_index: int
    block_id: str
    permutation_id: str
    order_position: int
    candidate_id: str
    approved_strategy_id: str
    started_at_utc: str
    client_materialization_latency_ms: float
    process_cpu_time_ms: float
    output_row_count: int
    semantic_result_digest: str


def load_bts_mask_join_pilot_config(path: Path | str) -> BtsMaskJoinPilotConfig:
    """Load the exact predeclared timing-protocol validation configuration."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Mask/Join pilot config must contain a JSON object")
    return BtsMaskJoinPilotConfig(
        results_dir=str(payload["results_dir"]),
        sample_rows=int(payload["sample_rows"]),
        warmup_blocks=int(payload["warmup_blocks"]),
        measured_blocks=int(payload["measured_blocks"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        absolute_half_drift_limit=float(payload["absolute_half_drift_limit"]),
        paired_ratio_half_drift_limit=float(payload["paired_ratio_half_drift_limit"]),
        paired_ratio_outlier_fraction_limit=float(payload["paired_ratio_outlier_fraction_limit"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        query_family_protocol_sha256=str(payload["query_family_protocol_sha256"]),
        semantic_smoke_sha256=str(payload["semantic_smoke_sha256"]),
        full_month=bool(payload.get("full_month", False)),
        require_clean_git=bool(payload.get("require_clean_git", False)),
        scientific_label=str(payload["scientific_label"]),
        paper_performance_evidence=bool(payload.get("paper_performance_evidence", False)),
        heldout_optimizer_evidence=bool(payload.get("heldout_optimizer_evidence", False)),
    )


def _environment(
    config: BtsMaskJoinPilotConfig,
    *,
    commit: str,
    dirty: bool,
) -> dict[str, Any]:
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
        "cache_protocol": "hot_same_duckdb_connection",
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _write_csv_atomic(path: Path, rows: list[MaskJoinTiming]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MaskJoinTiming.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    os.replace(temporary, path)


def _candidate_label(execution_mode: str) -> str:
    return LATE_CANDIDATE if execution_mode == "fused" else EARLY_CANDIDATE


def _verify_frozen_inputs(root: Path, config: BtsMaskJoinPilotConfig) -> None:
    bindings = (
        (
            root / "experiments/configs/real_data_query_families_v1.json",
            config.query_family_protocol_sha256,
        ),
        (
            root / "data/manifests/processed/bts-mask-join-semantic-smoke.json",
            config.semantic_smoke_sha256,
        ),
    )
    for path, expected in bindings:
        if not path.is_file() or sha256_file(path) != expected:
            raise GovernedRealDataSmokeError(f"Frozen Mask/Join input changed: {path}")


def run_bts_mask_join_pilot(
    config: BtsMaskJoinPilotConfig,
    *,
    project_root: Path,
    show_progress: bool = False,
) -> Path:
    """Run one atomic paired protocol validation and write auditable artifacts."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for Mask/Join pilot") from exc

    root = project_root.resolve()
    _verify_frozen_inputs(root, config)
    if config.paper_performance_evidence:
        freeze = audit_source_freeze(root)
        if freeze.status != "READY":
            raise GovernedRealDataSmokeError("formal Mask/Join timing requires source READY")
    artifacts = (
        verify_bts_mask_join_full_month_artifacts(root / "data")
        if config.full_month
        else verify_bts_mask_join_slice_artifacts(root / "data", config.sample_rows)
    )
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("Mask/Join pilot requires a clean worktree")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / config.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(run_dir / "config.json", asdict(config))
    _atomic_json(run_dir / "environment.json", _environment(config, commit=commit, dirty=dirty))
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})

    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_mask_join_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_mask_join_policy.json"))
    response = validate(
        _load_json(examples / "plans/bts_mask_join_placement.json"),
        policy,
        catalog,
    )
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("Frozen Mask/Join plan no longer validates")
    logical: ValidatedLogicalPlan = response.validated_plan
    mask = next(operator for operator in logical.operators if isinstance(operator, Mask))
    approved = generate_duckdb_candidates(
        logical,
        operator_placements=((mask.operator_id, BTS_MASK_JOIN_TARGET),),
    )

    connection = duckdb.connect()
    compiled: dict[str, CompiledQuery] = {}
    plans: dict[str, dict[str, Any]] = {}
    expected_digest: str | None = None
    timings: list[MaskJoinTiming] = []
    total_steps = 2 + 2 * (config.warmup_blocks + config.measured_blocks)
    progress = _Progress(total_steps, show_progress)
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb-mask-join-pilot"
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(temp_dir)}")
        bindings = _create_bts_mask_join_views(
            connection,
            root / "data",
            sample_rows=config.sample_rows,
            full_month=config.full_month,
        )
        filtered_rows = _filtered_row_count(connection)
        exposures: list[CandidateExposure] = []
        fingerprints: set[str] = set()
        strategy_ids: dict[str, str] = {}
        for candidate in approved:
            candidate_id = _candidate_label(candidate.strategy.execution_mode)
            strategy_ids[candidate_id] = candidate.strategy.strategy_id
            exposure = CandidateExposure(
                candidate_id,
                filtered_rows if candidate_id == LATE_CANDIDATE else 0,
                0,
                0,
            )
            exposures.append(exposure)
            query = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(query, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError("Mask/Join preflight outputs differ")
            certificate_status = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"mask-join-pilot-{run_id}-{candidate_id}",
            )
            observation = observe_duckdb_plan(
                connection,
                query.sql,
                query.parameters,
                analyze=True,
            )
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError("Mask/Join preflight plans collapsed")
            fingerprints.add(observation.fingerprint)
            compiled[candidate_id] = query
            plans[candidate_id] = {
                "approved_strategy_id": candidate.strategy.strategy_id,
                "physical_plan_id": candidate.physical_plan_id,
                "duckdb_plan_fingerprint": observation.fingerprint,
                "duckdb_operator_names": list(observation.operator_names),
                "actual_cardinalities": list(observation.actual_cardinalities),
                "rows_scanned": list(observation.rows_scanned),
                "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                "certificate_status": certificate_status,
                "exposure": asdict(exposure),
            }
            progress.advance(f"preflight {candidate_id}")

        feasibility = {
            profile.policy_id: filter_feasible_candidates(exposures, profile)
            for profile in (
                GovernanceFeasibilityPolicy("raw-join-permitted", None, 0),
                GovernanceFeasibilityPolicy("no-raw-sensitive-join", 0, 0),
            )
        }
        candidate_ids = (LATE_CANDIDATE, EARLY_CANDIDATE)
        warmup_orders = complete_permutation_orders(
            candidate_ids,
            config.warmup_blocks,
            seed=config.order_seed,
        )
        measured_orders = complete_permutation_orders(
            candidate_ids,
            config.measured_blocks,
            seed=config.order_seed + 1,
        )
        schedule = [(False, index, order) for index, order in enumerate(warmup_orders)] + [
            (True, index, order) for index, order in enumerate(measured_orders)
        ]
        for measured, block_index, order in schedule:
            permutation_id = " -> ".join(order)
            block_id = f"mask-join-block-{block_index:03d}"
            for position, candidate_id in enumerate(order):
                started_at = datetime.now(UTC).isoformat()
                cpu_started = time.process_time_ns()
                started = time.perf_counter_ns()
                execution = execute_with_connection(compiled[candidate_id], connection)
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
                digest = _semantic_digest(execution.columns, execution.rows)
                if digest != expected_digest:
                    raise GovernedRealDataSmokeError("Mask/Join timed result changed")
                if measured:
                    timings.append(
                        MaskJoinTiming(
                            block_index=block_index,
                            block_id=block_id,
                            permutation_id=permutation_id,
                            order_position=position,
                            candidate_id=candidate_id,
                            approved_strategy_id=strategy_ids[candidate_id],
                            started_at_utc=started_at,
                            client_materialization_latency_ms=latency_ms,
                            process_cpu_time_ms=cpu_ms,
                            output_row_count=execution.row_count,
                            semantic_result_digest=digest,
                        )
                    )
                progress.advance(f"{'measure' if measured else 'warmup'} {candidate_id}")
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

    by_candidate = {
        candidate_id: [
            row.client_materialization_latency_ms
            for row in timings
            if row.candidate_id == candidate_id
        ]
        for candidate_id in (LATE_CANDIDATE, EARLY_CANDIDATE)
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
    _write_csv_atomic(run_dir / "measurements.csv", timings)
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
            "cache_protocol": "hot_same_duckdb_connection",
            "sample_rows": config.sample_rows,
            "full_month": config.full_month,
            "filtered_rows_entering_join": filtered_rows,
            "candidate_count": len(compiled),
            "distinct_duckdb_plan_count": len(fingerprints),
            "verified_execution_artifacts": [asdict(item) for item in artifacts],
            "candidate_summaries": summaries,
            "governance_profiles": {
                name: {
                    "status": result.status,
                    "feasible_candidate_ids": list(result.feasible_candidate_ids),
                    "rejected_candidate_ids": list(result.rejected_candidate_ids),
                    "decisions": [asdict(item) for item in result.decisions],
                }
                for name, result in feasibility.items()
            },
            "measurement_count": len(timings),
        },
    )
    return run_dir
