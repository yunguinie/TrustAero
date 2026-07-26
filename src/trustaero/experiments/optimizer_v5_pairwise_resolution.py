"""Connection-isolated pairwise resolution for inconclusive V5 comparisons.

Each compared candidate owns a persistent DuckDB connection.  This prevents a
third physical route from changing the connection-local state seen by either
member of the pair.  Execution order is still balanced inside paired blocks to
control machine-wide drift.  Only comparisons that were inconclusive in the
frozen V5 V2 result are eligible for this bounded follow-up.
"""

from __future__ import annotations

import csv
import hashlib
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
from typing import Any, cast

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_real_data_slice_artifacts
from trustaero.execution import (
    CompiledQuery,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.optimizer_v5_calibration_analysis import (
    _authorized_oracle_set,
)
from trustaero.experiments.paired_claims import stratified_paired_bootstrap_ci
from trustaero.experiments.real_data_candidates import (
    _TARGETS,
    _candidate_exposure,
    _raw_plan,
    verify_candidate_execution_certificate,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _create_trusted_views,
    _load_json,
)
from trustaero.experiments.real_data_pilot import (
    _git_state,
    _percentile,
    _Progress,
    _semantic_digest,
    _stage_statistics,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import ApprovedPhysicalPlan, PolicySet, ValidatedLogicalPlan
from trustaero.optimizer.candidate_feasibility import (
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility.source_freeze import sha256_file
from trustaero.validator.service import validate

BASELINE_ID = "fused"
EXPECTED_UNRESOLVED_PAIRS = {
    ("bts-n100000", "materialize-after-bts-filter"),
    ("bts-n100000", "materialize-after-gov-002-mask"),
    ("bts-n500000", "materialize-after-gov-002-mask"),
    ("nyc_tlc-n500000", "materialize-after-nyc-zone-join"),
}


@dataclass(frozen=True, slots=True)
class PairwiseResolutionSpec:
    pair_id: str
    unit_id: str
    workload: str
    sample_rows: int
    candidate_id: str

    def __post_init__(self) -> None:
        if not self.pair_id or self.workload not in _TARGETS:
            raise ValueError("Pairwise resolution identity is invalid")
        if self.sample_rows < 1 or self.candidate_id == BASELINE_ID:
            raise ValueError("Pairwise resolution requires a non-baseline candidate")


@dataclass(frozen=True, slots=True)
class PairwiseResolutionConfig:
    protocol_name: str
    results_dir: str
    pairs: tuple[PairwiseResolutionSpec, ...]
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    confidence_level: float
    bootstrap_repetitions: int
    bootstrap_seed: int
    tie_threshold_fraction: float
    minimum_model_eligible_units: int
    require_clean_git: bool
    v2_inference_path: str
    v2_inference_sha256: str
    v2_negative_record_path: str
    v2_negative_record_sha256: str
    query_family_protocol_path: str
    query_family_protocol_sha256: str
    scientific_boundary: str

    def __post_init__(self) -> None:
        if not self.protocol_name or not self.results_dir:
            raise ValueError("Pairwise protocol identity is required")
        pair_keys = {(item.unit_id, item.candidate_id) for item in self.pairs}
        if pair_keys != EXPECTED_UNRESOLVED_PAIRS:
            raise ValueError("Pairwise protocol must cover exactly the frozen unresolved set")
        if len({item.pair_id for item in self.pairs}) != len(self.pairs):
            raise ValueError("Pair IDs must be unique")
        if self.warmup_blocks < 2 or self.warmup_blocks % 2:
            raise ValueError("Pairwise warmups must balance both orders")
        if self.measured_blocks < 40 or self.measured_blocks % 2:
            raise ValueError("Pairwise inference requires at least 40 balanced blocks")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 512:
            raise ValueError("Pairwise DuckDB controls are invalid")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("Pairwise confidence level is invalid")
        if self.bootstrap_repetitions < 1000:
            raise ValueError("Pairwise bootstrap requires at least 1000 repetitions")
        if not 0.0 <= self.tie_threshold_fraction < 1.0:
            raise ValueError("Pairwise tie threshold is invalid")
        if self.minimum_model_eligible_units < 2:
            raise ValueError("Pairwise merge must require at least two eligible units")
        digests = (
            self.v2_inference_sha256,
            self.v2_negative_record_sha256,
            self.query_family_protocol_sha256,
        )
        if any(len(item) != 64 for item in digests):
            raise ValueError("Pairwise source bindings must be SHA-256")


@dataclass(frozen=True, slots=True)
class PairwiseTiming:
    pair_id: str
    unit_id: str
    workload: str
    sample_rows: int
    block_index: int
    permutation_id: str
    order_position: int
    candidate_id: str
    started_at_utc: str
    latency_ms: float
    process_cpu_time_ms: float
    output_row_count: int
    semantic_result_digest: str


def load_pairwise_resolution_config(
    path: Path | str,
) -> PairwiseResolutionConfig:
    """Load the frozen, bounded unresolved-pair protocol."""

    payload = _load_json(Path(path))
    pairs = tuple(
        PairwiseResolutionSpec(
            pair_id=str(item["pair_id"]),
            unit_id=str(item["unit_id"]),
            workload=str(item["workload"]),
            sample_rows=int(item["sample_rows"]),
            candidate_id=str(item["candidate_id"]),
        )
        for item in cast(list[dict[str, Any]], payload["pairs"])
    )
    return PairwiseResolutionConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        pairs=pairs,
        warmup_blocks=int(payload["warmup_blocks"]),
        measured_blocks=int(payload["measured_blocks"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_repetitions=int(payload["bootstrap_repetitions"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        minimum_model_eligible_units=int(payload["minimum_model_eligible_units"]),
        require_clean_git=bool(payload["require_clean_git"]),
        v2_inference_path=str(payload["v2_inference_path"]),
        v2_inference_sha256=str(payload["v2_inference_sha256"]),
        v2_negative_record_path=str(payload["v2_negative_record_path"]),
        v2_negative_record_sha256=str(payload["v2_negative_record_sha256"]),
        query_family_protocol_path=str(payload["query_family_protocol_path"]),
        query_family_protocol_sha256=str(payload["query_family_protocol_sha256"]),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def balanced_pair_orders(
    baseline_id: str,
    candidate_id: str,
    block_count: int,
    *,
    seed: int,
) -> tuple[tuple[str, str], ...]:
    """Return a deterministic shuffle with exactly equal two-order counts."""

    if block_count < 0 or block_count % 2:
        raise ValueError("Pair orders require a nonnegative even block count")
    orders = [(baseline_id, candidate_id)] * (block_count // 2)
    orders.extend([(candidate_id, baseline_id)] * (block_count // 2))
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def classify_pair_ratio(
    lower: float,
    upper: float,
    *,
    tie_fraction: float,
) -> str:
    """Classify one candidate/fused CI without using its point estimate alone."""

    if upper < 1.0 - tie_fraction:
        return "MATERIALLY_FASTER"
    if lower > 1.0 + tie_fraction:
        return "MATERIALLY_SLOWER"
    if lower >= 1.0 - tie_fraction and upper <= 1.0 + tie_fraction:
        return "PRACTICALLY_EQUIVALENT"
    return "INCONCLUSIVE"


def _stable_seed(seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _profiles() -> tuple[GovernanceFeasibilityPolicy, ...]:
    return (
        GovernanceFeasibilityPolicy("output-mask-only", None, None),
        GovernanceFeasibilityPolicy("no-raw-sensitive-materialization", None, 0),
    )


def _logical_candidates(
    root: Path,
    workload: str,
) -> tuple[ValidatedLogicalPlan, InMemoryCatalog, dict[str, ApprovedPhysicalPlan]]:
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    response = validate(_raw_plan(examples, workload), policy, catalog)
    if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
        raise GovernedRealDataSmokeError("Pairwise logical plan was not approved")
    logical = response.validated_plan
    if logical is None:
        raise GovernedRealDataSmokeError("Pairwise logical plan is missing")
    candidates = generate_duckdb_candidates(
        logical,
        materialization_targets=_TARGETS[workload],
    )
    return logical, catalog, {candidate.strategy.strategy_id: candidate for candidate in candidates}


def _configure_connection(
    connection: Any,
    *,
    root: Path,
    pair_id: str,
    candidate_id: str,
    threads: int,
    memory_limit_mb: int,
) -> None:
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute(f"SET threads = {threads}")
    connection.execute(f"SET memory_limit = '{memory_limit_mb}MB'")
    safe_candidate = candidate_id.replace("/", "-").replace("\\", "-")
    spill = root / "data/tmp/duckdb" / f"v5-pair-{pair_id}-{safe_candidate}"
    spill.mkdir(parents=True, exist_ok=True)
    escaped = str(spill).replace("'", "''")
    connection.execute(f"SET temp_directory = '{escaped}'")


def _run_pair(
    *,
    root: Path,
    config: PairwiseResolutionConfig,
    spec: PairwiseResolutionSpec,
    progress: _Progress,
) -> dict[str, Any]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for V5 pairs") from exc

    artifacts = verify_real_data_slice_artifacts(root / "data", spec.sample_rows)
    logical, catalog, candidates = _logical_candidates(root, spec.workload)
    ids = (BASELINE_ID, spec.candidate_id)
    if any(candidate_id not in candidates for candidate_id in ids):
        raise GovernedRealDataSmokeError(f"Unknown pairwise candidate: {spec.pair_id}")
    connections: dict[str, Any] = {}
    compiled: dict[str, CompiledQuery] = {}
    plans: dict[str, dict[str, Any]] = {}
    expected_digest: str | None = None
    stage_statistics: dict[str, int | float] | None = None
    try:
        for candidate_id in ids:
            connection = duckdb.connect()
            connections[candidate_id] = connection
            _configure_connection(
                connection,
                root=root,
                pair_id=spec.pair_id,
                candidate_id=candidate_id,
                threads=config.duckdb_threads,
                memory_limit_mb=config.duckdb_memory_limit_mb,
            )
            bindings = _create_trusted_views(
                connection,
                root / "data",
                sample_rows=spec.sample_rows,
            )
            if stage_statistics is None:
                stage_statistics = _stage_statistics(connection, spec.workload)
            candidate = candidates[candidate_id]
            query = compile_approved_physical_plan(
                logical,
                candidate,
                catalog,
                bindings,
            )
            execution = execute_with_connection(query, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError(f"{spec.pair_id} outputs differ")
            certificate = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"v5-pair-{spec.pair_id}-{candidate_id}",
            )
            observed = observe_duckdb_plan(
                connection,
                query.sql,
                query.parameters,
                analyze=True,
            )
            compiled[candidate_id] = query
            plans[candidate_id] = {
                "physical_plan_id": candidate.physical_plan_id,
                "duckdb_plan_fingerprint": observed.fingerprint,
                "duckdb_operator_names": list(observed.operator_names),
                "actual_cardinalities": list(observed.actual_cardinalities),
                "rows_scanned": list(observed.rows_scanned),
                "peak_buffer_memory_bytes": observed.peak_buffer_memory_bytes,
                "peak_temp_directory_bytes": observed.peak_temp_directory_bytes,
                "certificate_status": certificate,
                "connection_isolation": "dedicated_persistent_connection",
            }
            progress.advance(f"{spec.pair_id} preflight {candidate_id}")

        if plans[ids[0]]["duckdb_plan_fingerprint"] == plans[ids[1]]["duckdb_plan_fingerprint"]:
            raise GovernedRealDataSmokeError(f"{spec.pair_id} plans collapsed")
        governed_rows = int(cast(dict[str, Any], stage_statistics)["governed_rows"])
        exposures = {
            candidate_id: _candidate_exposure(
                workload=spec.workload,
                strategy_id=candidate_id,
                materialize_after=candidates[candidate_id].strategy.materialize_after,
                governed_rows=governed_rows,
            )
            for candidate_id in ids
        }
        feasible_profiles = [
            profile.policy_id
            for profile in _profiles()
            if spec.candidate_id
            in filter_feasible_candidates(tuple(exposures.values()), profile).feasible_candidate_ids
        ]
        if not feasible_profiles:
            raise GovernedRealDataSmokeError(f"{spec.pair_id} is never governance-feasible")

        offset = _stable_seed(config.order_seed, spec.pair_id)
        warmups = balanced_pair_orders(
            BASELINE_ID,
            spec.candidate_id,
            config.warmup_blocks,
            seed=offset + 1,
        )
        measured = balanced_pair_orders(
            BASELINE_ID,
            spec.candidate_id,
            config.measured_blocks,
            seed=offset + 2,
        )
        timings: list[PairwiseTiming] = []
        schedule = [(False, index, order) for index, order in enumerate(warmups)]
        schedule.extend((True, index, order) for index, order in enumerate(measured))
        for is_measured, block_index, order in schedule:
            permutation = " -> ".join(order)
            for position, candidate_id in enumerate(order):
                started_at = datetime.now(UTC).isoformat()
                cpu_started = time.process_time_ns()
                started = time.perf_counter_ns()
                execution = execute_with_connection(
                    compiled[candidate_id],
                    connections[candidate_id],
                )
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
                digest = _semantic_digest(execution.columns, execution.rows)
                if digest != expected_digest:
                    raise GovernedRealDataSmokeError(f"{spec.pair_id} timed output changed")
                if is_measured:
                    timings.append(
                        PairwiseTiming(
                            pair_id=spec.pair_id,
                            unit_id=spec.unit_id,
                            workload=spec.workload,
                            sample_rows=spec.sample_rows,
                            block_index=block_index,
                            permutation_id=permutation,
                            order_position=position,
                            candidate_id=candidate_id,
                            started_at_utc=started_at,
                            latency_ms=latency_ms,
                            process_cpu_time_ms=cpu_ms,
                            output_row_count=execution.row_count,
                            semantic_result_digest=digest,
                        )
                    )
                kind = "measured" if is_measured else "warmup"
                progress.advance(f"{spec.pair_id} {kind} {candidate_id}")
    finally:
        for connection in connections.values():
            connection.close()

    by_candidate = {
        candidate_id: [item.latency_ms for item in timings if item.candidate_id == candidate_id]
        for candidate_id in ids
    }
    ratios_by_order: dict[str, list[float]] = {}
    for block_index in range(config.measured_blocks):
        block = [item for item in timings if item.block_index == block_index]
        values = {item.candidate_id: item.latency_ms for item in block}
        permutation_id = next(item.permutation_id for item in block)
        ratios_by_order.setdefault(permutation_id, []).append(
            values[spec.candidate_id] / values[BASELINE_ID]
        )
    lower, upper = stratified_paired_bootstrap_ci(
        ratios_by_order,
        confidence_level=config.confidence_level,
        repetitions=config.bootstrap_repetitions,
        seed=_stable_seed(config.bootstrap_seed, spec.pair_id),
    )
    ratios = [value for values in ratios_by_order.values() for value in values]
    conclusion = classify_pair_ratio(
        lower,
        upper,
        tie_fraction=config.tie_threshold_fraction,
    )
    summaries = {
        candidate_id: {
            "median_ms": statistics.median(values),
            "p95_ms": _percentile(values, 0.95),
            "min_ms": min(values),
            "max_ms": max(values),
            **plans[candidate_id],
        }
        for candidate_id, values in by_candidate.items()
    }
    return {
        "pair_id": spec.pair_id,
        "unit_id": spec.unit_id,
        "workload": spec.workload,
        "sample_rows": spec.sample_rows,
        "status": "PASS",
        "candidate_ids": list(ids),
        "feasible_policy_profiles": feasible_profiles,
        "stage_statistics": stage_statistics,
        "verified_execution_artifacts": [asdict(item) for item in artifacts],
        "candidate_summaries": summaries,
        "paired_claim": {
            "candidate_id": spec.candidate_id,
            "baseline_id": BASELINE_ID,
            "connection_isolation": "one_persistent_connection_per_candidate",
            "paired_block_count": len(ratios),
            "order_stratum_counts": {
                key: len(values) for key, values in sorted(ratios_by_order.items())
            },
            "median_candidate_over_baseline_ratio": statistics.median(ratios),
            "confidence_interval": {
                "method": "order_stratified_paired_bootstrap_median_ratio_v1",
                "level": config.confidence_level,
                "lower": lower,
                "upper": upper,
                "repetitions": config.bootstrap_repetitions,
            },
            "tie_fraction": config.tie_threshold_fraction,
            "conclusion": conclusion,
            "claim_authorized": conclusion != "INCONCLUSIVE",
        },
        "measurements": [asdict(item) for item in timings],
    }


def merge_pairwise_labels(
    v2_inference: dict[str, Any],
    pair_results: list[dict[str, Any]],
    *,
    minimum_model_eligible_units: int,
) -> dict[str, object]:
    """Replace only frozen inconclusive claims, then apply profile legality."""

    pair_claims = {
        (str(item["unit_id"]), str(item["paired_claim"]["candidate_id"])): cast(
            dict[str, Any], item["paired_claim"]
        )
        for item in pair_results
    }
    labels: list[dict[str, object]] = []
    observations = cast(
        list[dict[str, Any]],
        v2_inference["legacy_stability_diagnostics"]["observations"],
    )
    feasible_by_unit_profile = {
        (str(item["unit_id"]), str(item["policy_profile"])): tuple(
            str(candidate) for candidate in item["feasible_candidate_ids"]
        )
        for item in observations
    }
    for unit in cast(list[dict[str, Any]], v2_inference["unit_results"]):
        unit_id = str(unit["unit_id"])
        claims = {
            str(item["candidate_id"]): dict(item)
            for item in cast(list[dict[str, Any]], unit["paired_claims"])
        }
        for candidate_id in tuple(claims):
            replacement = pair_claims.get((unit_id, candidate_id))
            if replacement is not None:
                if bool(claims[candidate_id]["claim_authorized"]):
                    raise ValueError("Pairwise result attempted to replace a conclusive claim")
                claims[candidate_id] = replacement
        profiles = sorted(
            profile
            for observed_unit, profile in feasible_by_unit_profile
            if observed_unit == unit_id
        )
        for profile in profiles:
            feasible = feasible_by_unit_profile[(unit_id, profile)]
            relevant = [claim for candidate_id, claim in claims.items() if candidate_id in feasible]
            oracle_set = _authorized_oracle_set(relevant)
            labels.append(
                {
                    "unit_id": unit_id,
                    "policy_profile": profile,
                    "feasible_candidate_ids": list(feasible),
                    "authorized_oracle_set": oracle_set,
                    "model_label_authorized": oracle_set is not None,
                    "candidate_claims": relevant,
                }
            )
    eligible_units = sorted(
        {str(item["unit_id"]) for item in labels if item["model_label_authorized"]}
    )
    gates = {
        "all_frozen_unresolved_pairs_completed": (set(pair_claims) == EXPECTED_UNRESOLVED_PAIRS),
        "minimum_model_eligible_units": (len(eligible_units) >= minimum_model_eligible_units),
        "governance_profiles_applied_before_labels": True,
        "external_partition_not_accessed": True,
    }
    return {
        "schema_version": 1,
        "status": (
            "PASS_V5_PAIRWISE_LABEL_GATE"
            if all(gates.values())
            else "FAIL_V5_PAIRWISE_LABEL_GATE_RETAIN"
        ),
        "gate_checks": gates,
        "model_eligible_unit_ids": eligible_units,
        "model_eligible_unit_count": len(eligible_units),
        "profile_labels": labels,
        "point_estimate_substitution_used": False,
        "external_partition_accessed": False,
    }


def _environment(config: PairwiseResolutionConfig, commit: str, dirty: bool) -> dict[str, Any]:
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
        "duckdb_threads_per_connection": config.duckdb_threads,
        "duckdb_memory_limit_mb_per_connection": config.duckdb_memory_limit_mb,
        "connection_count_per_pair": 2,
        "gpu_acceleration": False,
    }


def _write_measurements(run_dir: Path, results: list[dict[str, Any]]) -> None:
    rows = [row for result in results for row in result["measurements"]]
    with (run_dir / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PairwiseTiming.__annotations__))
        writer.writeheader()
        writer.writerows(rows)


def run_optimizer_v5_pairwise_resolution(
    config: PairwiseResolutionConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or safely resume the four frozen unresolved comparisons."""

    root = project_root.resolve()
    bindings = (
        (config.v2_inference_path, config.v2_inference_sha256),
        (config.v2_negative_record_path, config.v2_negative_record_sha256),
        (config.query_family_protocol_path, config.query_family_protocol_sha256),
    )
    for relative, expected in bindings:
        if sha256_file(root / relative) != expected:
            raise GovernedRealDataSmokeError(f"Pairwise binding changed: {relative}")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("Pairwise resolution requires a clean commit")
    results_root = root / config.results_dir
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = results_root / run_id
    units_dir = run_dir / "pairs"
    units_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(json.dumps(asdict(config), sort_keys=True))
    config_path = run_dir / "config.json"
    if resume_run_id and config_path.is_file():
        if _load_json(config_path) != payload:
            raise GovernedRealDataSmokeError("Pairwise resume config changed")
    _atomic_json(config_path, payload)
    _atomic_json(run_dir / "environment.json", _environment(config, commit, dirty))
    if resume_run_id is None:
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    completed = {path.stem for path in units_dir.glob("*.json")}
    pending = [item for item in config.pairs if item.pair_id not in completed]
    steps = 2 + 2 * (config.warmup_blocks + config.measured_blocks)
    progress = _Progress(len(pending) * steps, show_progress)
    for spec in pending:
        result = _run_pair(root=root, config=config, spec=spec, progress=progress)
        _atomic_json(units_dir / f"{spec.pair_id}.json", result)
        completed.add(spec.pair_id)
        _atomic_json(
            run_dir / "progress.json",
            {
                "run_id": run_id,
                "completed_pairs": len(completed),
                "total_pairs": len(config.pairs),
                "last_completed_pair": spec.pair_id,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            },
        )
    results = [_load_json(path) for path in sorted(units_dir.glob("*.json"))]
    if len(results) != len(config.pairs):
        raise GovernedRealDataSmokeError("Pairwise resolution is incomplete")
    _write_measurements(run_dir, results)
    v2 = _load_json(root / config.v2_inference_path)
    merged = merge_pairwise_labels(
        v2,
        results,
        minimum_model_eligible_units=config.minimum_model_eligible_units,
    )
    _atomic_json(run_dir / "merged_inference.json", merged)
    _atomic_json(
        run_dir / "summary.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": merged["status"],
            "completed_pairs": len(results),
            "expected_pairs": len(config.pairs),
            "measured_candidate_executions": sum(len(item["measurements"]) for item in results),
            "pair_results": [
                {key: value for key, value in item.items() if key != "measurements"}
                for item in results
            ],
            "model_eligible_unit_count": merged["model_eligible_unit_count"],
            "paper_performance_evidence": False,
            "heldout_optimizer_evidence": False,
            "optimizer_selection_evaluated": False,
            "external_partition_accessed": False,
            "scientific_boundary": config.scientific_boundary,
        },
    )
    return run_dir
