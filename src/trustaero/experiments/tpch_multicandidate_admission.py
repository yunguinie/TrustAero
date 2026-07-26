"""Balanced SF1 admission for structurally different TPC-H candidates.

This is a development gate, not final paper performance evidence.  It asks
whether Q3 and Q10 expose stable, query-dependent physical winners before the
project spends time on SF10 or trains a cross-query selector.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import random
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import (
    CompiledQuery,
    TableBindings,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_governed import GovernedRealDataSmokeError, _atomic_json
from trustaero.experiments.tpch_audit import tpch_git_state, verify_tpch_artifact
from trustaero.experiments.tpch_query_coverage_v2 import _create_bindings
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate


@dataclass(frozen=True, slots=True)
class TpchMulticandidateAdmissionConfig:
    """Frozen candidate set, timing controls, and admission thresholds."""

    results_dir: str
    scale_factor: int
    semantic_result_path: str
    semantic_result_sha256: str
    query_targets: tuple[tuple[str, tuple[str, ...]], ...]
    warmup_rounds: int
    measured_rounds_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    maximum_paired_ratio_half_drift: float
    minimum_distinct_singleton_winners: int
    require_clean_git: bool

    def __post_init__(self) -> None:
        if self.scale_factor != 1:
            raise ValueError("Admission is frozen to SF1")
        if len(self.semantic_result_sha256) != 64:
            raise ValueError("Semantic result must be SHA-256 bound")
        if tuple(query for query, _targets in self.query_targets) != ("q03", "q10"):
            raise ValueError("Admission must cover Q3 and Q10 exactly")
        if any(len(targets) != 3 or len(set(targets)) != 3 for _, targets in self.query_targets):
            raise ValueError("Each query requires exactly three materialization targets")
        if self.warmup_rounds < 1 or self.measured_rounds_per_permutation < 2:
            raise ValueError("Admission requires warmup and two rounds per permutation")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 512:
            raise ValueError("DuckDB controls are invalid")
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("Practical tie fraction is invalid")
        if not 0.0 < self.confidence_level < 1.0 or self.bootstrap_draws < 1_000:
            raise ValueError("Bootstrap controls are invalid")
        if not 0.0 <= self.maximum_paired_ratio_half_drift < 1.0:
            raise ValueError("Half-drift threshold is invalid")
        if self.minimum_distinct_singleton_winners != 2:
            raise ValueError("Two-query admission requires two distinct winners")


@dataclass(frozen=True, slots=True)
class TpchAdmissionMeasurement:
    """One candidate observation inside a complete paired block."""

    query_id: str
    block_index: int
    permutation_index: int
    repetition: int
    position: int
    candidate_id: str
    latency_ms: float
    row_count: int
    result_digest: str


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def load_tpch_multicandidate_admission_config(
    path: Path | str,
) -> TpchMulticandidateAdmissionConfig:
    """Load the strict preregistered admission protocol."""

    payload = _object(Path(path))
    targets = tuple(
        (str(query), tuple(str(value) for value in values))
        for query, values in dict(payload["query_targets"]).items()
    )
    return TpchMulticandidateAdmissionConfig(
        results_dir=str(payload["results_dir"]),
        scale_factor=int(payload["scale_factor"]),
        semantic_result_path=str(payload["semantic_result_path"]),
        semantic_result_sha256=str(payload["semantic_result_sha256"]),
        query_targets=targets,
        warmup_rounds=int(payload["warmup_rounds"]),
        measured_rounds_per_permutation=int(payload["measured_rounds_per_permutation"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_draws=int(payload["bootstrap_draws"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        maximum_paired_ratio_half_drift=float(payload["maximum_paired_ratio_half_drift"]),
        minimum_distinct_singleton_winners=int(payload["minimum_distinct_singleton_winners"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def _candidate_map(
    root: Path,
    query_id: str,
    targets: tuple[str, ...],
    catalog: InMemoryCatalog,
    policy: PolicySet,
    bindings: TableBindings,
) -> tuple[dict[str, CompiledQuery], str]:
    plan_path = root / f"examples/tpch/plans/{query_id}.json"
    response = validate(_object(plan_path), policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError(f"{query_id.upper()} no longer validates as REWRITE")
    physical = generate_duckdb_candidates(
        response.validated_plan,
        materialization_targets=targets,
    )
    if len(physical) != 4:
        raise GovernedRealDataSmokeError(f"{query_id.upper()} candidate space changed")
    compiled = {
        candidate.strategy.strategy_id: compile_approved_physical_plan(
            response.validated_plan, candidate, catalog, bindings
        )
        for candidate in physical
    }
    return compiled, response.validated_plan.logical_plan_id


def _write_measurements(path: Path, rows: list[TpchAdmissionMeasurement]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TpchAdmissionMeasurement.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def _bootstrap_ratio_interval(
    numerator: list[float],
    denominator: list[float],
    *,
    confidence_level: float,
    draws: int,
    seed: int,
) -> tuple[float, float]:
    """Bootstrap the paired median latency ratio by complete block."""

    if len(numerator) != len(denominator) or not numerator:
        raise ValueError("Paired bootstrap inputs are incomplete")
    ratios = [left / right for left, right in zip(numerator, denominator, strict=True)]
    rng = random.Random(seed)
    estimates = []
    for _ in range(draws):
        sample = [ratios[rng.randrange(len(ratios))] for _ in ratios]
        estimates.append(statistics.median(sample))
    estimates.sort()
    alpha = (1.0 - confidence_level) / 2.0
    low = estimates[int(alpha * (draws - 1))]
    high = estimates[int((1.0 - alpha) * (draws - 1))]
    return low, high


def _half_drift(values: list[float]) -> float:
    midpoint = len(values) // 2
    first = statistics.median(values[:midpoint])
    second = statistics.median(values[midpoint:])
    return abs(second / first - 1.0)


def _analyze_query(
    query_id: str,
    rows: list[TpchAdmissionMeasurement],
    config: TpchMulticandidateAdmissionConfig,
) -> dict[str, Any]:
    candidates = tuple(sorted({row.candidate_id for row in rows}))
    blocks = tuple(sorted({row.block_index for row in rows}))
    by_candidate = {
        candidate: [
            next(
                row.latency_ms
                for row in rows
                if row.block_index == block and row.candidate_id == candidate
            )
            for block in blocks
        ]
        for candidate in candidates
    }
    medians = {candidate: statistics.median(values) for candidate, values in by_candidate.items()}
    fastest = min(candidates, key=lambda candidate: (medians[candidate], candidate))
    comparisons: dict[str, Any] = {}
    singleton = True
    materiality = 1.0 + config.practical_tie_fraction
    for offset, candidate in enumerate(candidates):
        if candidate == fastest:
            continue
        low, high = _bootstrap_ratio_interval(
            by_candidate[candidate],
            by_candidate[fastest],
            confidence_level=config.confidence_level,
            draws=config.bootstrap_draws,
            seed=config.bootstrap_seed + offset,
        )
        comparisons[candidate] = {
            "ratio_to_fastest": medians[candidate] / medians[fastest],
            "paired_ratio_ci": [low, high],
            "materially_slower": low > materiality,
        }
        singleton = singleton and low > materiality
    drift = {
        candidate: _half_drift(
            [
                value / reference
                for value, reference in zip(values, by_candidate["fused"], strict=True)
            ]
        )
        for candidate, values in by_candidate.items()
        if candidate != "fused"
    }
    return {
        "query_id": query_id.upper(),
        "block_count": len(blocks),
        "candidate_median_ms": medians,
        "diagnostic_fastest_candidate": fastest,
        "singleton_winner": fastest if singleton else None,
        "comparisons_to_fastest": comparisons,
        "paired_ratio_half_drift": drift,
        "stability_pass": max(drift.values(), default=0.0)
        <= config.maximum_paired_ratio_half_drift,
    }


def run_tpch_multicandidate_admission(
    project_root: Path,
    config: TpchMulticandidateAdmissionConfig,
    *,
    progress: Callable[[int, int, str, float, float], None] | None = None,
) -> Path:
    """Run the balanced development admission and return its summary path."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for admission") from exc

    root = project_root.resolve()
    commit, dirty = tpch_git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("Admission requires a clean committed tree")
    database, artifact = verify_tpch_artifact(root, scale_factor=config.scale_factor)
    semantic_path = root / config.semantic_result_path
    if hashlib.sha256(semantic_path.read_bytes()).hexdigest() != config.semantic_result_sha256:
        raise GovernedRealDataSmokeError("Frozen Q3/Q10 semantic evidence changed")
    semantic = _object(semantic_path)
    expected_digests = {
        str(query["query_id"]).lower(): str(query["candidates"][0]["result_digest"])
        for query in semantic["queries"]
    }
    examples = root / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_object(examples / "catalog_v2.json")))
    policy = PolicySet.model_validate(_object(examples / "policy_v2.json"))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / config.results_dir / run_id
    output.mkdir(parents=True, exist_ok=False)

    connection = duckdb.connect(str(database), read_only=True)
    all_rows: list[TpchAdmissionMeasurement] = []
    plan_records: dict[str, Any] = {}
    start = time.perf_counter()
    permutations_per_query = 24 * config.measured_rounds_per_permutation
    total = len(config.query_targets) * permutations_per_query * 4
    completed = 0
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        bindings = _create_bindings(connection)
        for query_offset, (query_id, targets) in enumerate(config.query_targets):
            candidates, logical_plan_id = _candidate_map(
                root, query_id, targets, catalog, policy, bindings
            )
            candidate_ids = tuple(sorted(candidates))
            expected_digest: str | None = None
            fingerprints: set[str] = set()
            observed: list[dict[str, Any]] = []
            for candidate_id in candidate_ids:
                compiled = candidates[candidate_id]
                execution = execute_with_connection(compiled, connection)
                expected_digest = expected_digest or expected_digests[query_id]
                if execution.result_digest != expected_digest:
                    raise GovernedRealDataSmokeError(
                        f"{query_id.upper()} preflight differs from frozen official result"
                    )
                observation = observe_duckdb_plan(
                    connection, compiled.sql, compiled.parameters, analyze=True
                )
                fingerprints.add(observation.fingerprint)
                observed.append(
                    {
                        "candidate_id": candidate_id,
                        "fingerprint": observation.fingerprint,
                        "operator_names": list(observation.operator_names),
                        "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                        "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                    }
                )
            if len(fingerprints) != len(candidate_ids):
                raise GovernedRealDataSmokeError(f"{query_id.upper()} physical plans collapsed")
            if any(item["peak_temp_directory_bytes"] > 0 for item in observed):
                raise GovernedRealDataSmokeError(f"{query_id.upper()} preflight spilled to disk")
            plan_records[query_id] = {
                "logical_plan_id": logical_plan_id,
                "result_digest": expected_digest,
                "candidates": observed,
            }

            for round_index in range(config.warmup_rounds):
                shift = round_index % len(candidate_ids)
                order = candidate_ids[shift:] + candidate_ids[:shift]
                for candidate_id in order:
                    execute_with_connection(candidates[candidate_id], connection)

            permutations = list(itertools.permutations(candidate_ids))
            rng = random.Random(config.order_seed + query_offset)
            rng.shuffle(permutations)
            block_index = 0
            for repetition in range(config.measured_rounds_per_permutation):
                for permutation_index, order in enumerate(permutations):
                    block_index += 1
                    for position, candidate_id in enumerate(order, start=1):
                        started = time.perf_counter()
                        execution = execute_with_connection(candidates[candidate_id], connection)
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        if execution.result_digest != expected_digest:
                            raise GovernedRealDataSmokeError(
                                f"{query_id.upper()} timed result changed"
                            )
                        all_rows.append(
                            TpchAdmissionMeasurement(
                                query_id=query_id,
                                block_index=block_index,
                                permutation_index=permutation_index,
                                repetition=repetition,
                                position=position,
                                candidate_id=candidate_id,
                                latency_ms=latency_ms,
                                row_count=execution.row_count,
                                result_digest=execution.result_digest,
                            )
                        )
                        completed += 1
                        if progress and (completed == total or completed % 8 == 0):
                            elapsed = time.perf_counter() - start
                            eta = elapsed / completed * (total - completed)
                            progress(
                                completed,
                                total,
                                f"{query_id.upper()} block={block_index}",
                                elapsed,
                                eta,
                            )
    finally:
        connection.close()

    _write_measurements(output / "measurements.csv", all_rows)
    analyses = [
        _analyze_query(
            query_id,
            [row for row in all_rows if row.query_id == query_id],
            config,
        )
        for query_id, _targets in config.query_targets
    ]
    winners = [
        item["singleton_winner"] for item in analyses if item["singleton_winner"] is not None
    ]
    gates = {
        "all_queries_stable": all(item["stability_pass"] for item in analyses),
        "all_queries_have_singleton_winner": len(winners) == len(analyses),
        "distinct_singleton_winners": len(set(winners))
        >= config.minimum_distinct_singleton_winners,
        "all_results_equivalent": True,
        "all_physical_plans_distinct": True,
        "no_spill": True,
    }
    status = (
        "PASS_TPCH_MULTICANDIDATE_OPTIMIZER_ADMISSION"
        if all(gates.values())
        else "FAIL_TPCH_MULTICANDIDATE_OPTIMIZER_ADMISSION_RETAIN"
    )
    summary = {
        "schema_version": 1,
        "status": status,
        "source_commit": commit,
        "source_dirty": dirty,
        "artifact_sha256": artifact["sha256"],
        "config": asdict(config),
        "measurement_count": len(all_rows),
        "elapsed_seconds": time.perf_counter() - start,
        "queries": analyses,
        "singleton_winners": winners,
        "gates": gates,
        "plan_evidence": plan_records,
        "paper_performance_evidence": False,
        "scientific_boundary": (
            "Development admission only. The candidate set was selected after a "
            "single diagnostic mechanism probe; final SF10 claims require a new "
            "frozen protocol and independent measurements."
        ),
    }
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_id, "summary": str((output / "summary.json").relative_to(root))},
    )
    return output / "summary.json"
