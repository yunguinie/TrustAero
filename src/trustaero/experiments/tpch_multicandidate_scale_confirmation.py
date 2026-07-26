"""One-shot SF10 scale confirmation after the inconclusive SF1 admission.

The SF1 run was inspected before this protocol was written, so this stage is
explicitly development-scale confirmation.  It may establish stable
query-dependent winners, but it cannot be relabeled as an independent
optimizer evaluation.
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
    materialize_query_result,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_governed import GovernedRealDataSmokeError, _atomic_json
from trustaero.experiments.tpch_audit import tpch_git_state, verify_tpch_artifact
from trustaero.experiments.tpch_multicandidate_admission import (
    TpchAdmissionMeasurement,
    _bootstrap_ratio_interval,
    _half_drift,
)
from trustaero.experiments.tpch_query_coverage_v2 import _create_bindings
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate


@dataclass(frozen=True, slots=True)
class TpchScaleConfirmationConfig:
    """Frozen SF10 candidate set and paired measurement controls."""

    results_dir: str
    scale_factor: int
    prior_negative_path: str
    prior_negative_sha256: str
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
        if self.scale_factor != 10:
            raise ValueError("Scale confirmation is frozen to SF10")
        if len(self.prior_negative_sha256) != 64:
            raise ValueError("Prior SF1 negative must be SHA-256 bound")
        if tuple(query for query, _targets in self.query_targets) != ("q03", "q10"):
            raise ValueError("Scale confirmation must cover Q3 and Q10")
        if any(len(targets) != 2 or len(set(targets)) != 2 for _, targets in self.query_targets):
            raise ValueError("Each SF10 query requires exactly two materialized candidates")
        if self.warmup_rounds < 1 or self.measured_rounds_per_permutation < 5:
            raise ValueError("SF10 confirmation requires warmup and five rounds")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 2_048:
            raise ValueError("SF10 DuckDB controls are invalid")
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("Practical tie fraction is invalid")
        if not 0.0 < self.confidence_level < 1.0 or self.bootstrap_draws < 1_000:
            raise ValueError("Bootstrap controls are invalid")
        if not 0.0 <= self.maximum_paired_ratio_half_drift < 1.0:
            raise ValueError("Half-drift threshold is invalid")
        if self.minimum_distinct_singleton_winners != 2:
            raise ValueError("The two-query gate requires two distinct winners")


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def load_tpch_scale_confirmation_config(
    path: Path | str,
) -> TpchScaleConfirmationConfig:
    payload = _object(Path(path))
    return TpchScaleConfirmationConfig(
        results_dir=str(payload["results_dir"]),
        scale_factor=int(payload["scale_factor"]),
        prior_negative_path=str(payload["prior_negative_path"]),
        prior_negative_sha256=str(payload["prior_negative_sha256"]),
        query_targets=tuple(
            (str(query), tuple(str(target) for target in targets))
            for query, targets in dict(payload["query_targets"]).items()
        ),
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


def _bind_scale(value: Any, snapshot: str) -> Any:
    """Create explicit SF10 IR inputs from the reviewed SF1 templates.

    Only content-addressing labels change.  Relational operators, predicates,
    requested outputs, and policy obligations remain byte-for-byte equivalent
    after canonical JSON normalization.
    """

    if isinstance(value, dict):
        return {key: _bind_scale(item, snapshot) for key, item in value.items()}
    if isinstance(value, list):
        return [_bind_scale(item, snapshot) for item in value]
    if not isinstance(value, str):
        return value
    return (
        value.replace("tpch_sf1_", "tpch_sf10_")
        .replace("tpch-sf1", "tpch-sf10")
        .replace("sf1-64a709fa8f99", snapshot)
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _extension_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _compiled_candidates(
    root: Path,
    query_id: str,
    targets: tuple[str, ...],
    snapshot: str,
    bindings: TableBindings,
) -> tuple[dict[str, CompiledQuery], dict[str, Any]]:
    examples = root / "examples/tpch"
    catalog_payload = _bind_scale(_object(examples / "catalog_v2.json"), snapshot)
    policy_payload = _bind_scale(_object(examples / "policy_v2.json"), snapshot)
    plan_payload = _bind_scale(_object(examples / f"plans/{query_id}.json"), snapshot)
    catalog = InMemoryCatalog(CatalogDocument.model_validate(catalog_payload))
    policy = PolicySet.model_validate(policy_payload)
    response = validate(plan_payload, policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError(f"{query_id.upper()} SF10 binding no longer validates")
    physical = generate_duckdb_candidates(
        response.validated_plan,
        materialization_targets=targets,
    )
    if len(physical) != 3:
        raise GovernedRealDataSmokeError(f"{query_id.upper()} SF10 candidate space changed")
    compiled = {
        candidate.strategy.strategy_id: compile_approved_physical_plan(
            response.validated_plan, candidate, catalog, bindings
        )
        for candidate in physical
    }
    binding_record = {
        "catalog": catalog_payload,
        "policy": policy_payload,
        "plan": plan_payload,
        "validated_logical_plan_id": response.validated_plan.logical_plan_id,
        "bound_input_digest": _digest(
            {"catalog": catalog_payload, "policy": policy_payload, "plan": plan_payload}
        ),
    }
    return compiled, binding_record


def _write_measurements(path: Path, rows: list[TpchAdmissionMeasurement]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TpchAdmissionMeasurement.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def _analyze(
    query_id: str,
    rows: list[TpchAdmissionMeasurement],
    config: TpchScaleConfirmationConfig,
) -> dict[str, Any]:
    candidate_ids = tuple(sorted({row.candidate_id for row in rows}))
    blocks = tuple(sorted({row.block_index for row in rows}))
    values = {
        candidate: [
            next(
                row.latency_ms
                for row in rows
                if row.block_index == block and row.candidate_id == candidate
            )
            for block in blocks
        ]
        for candidate in candidate_ids
    }
    medians = {
        candidate: statistics.median(candidate_values)
        for candidate, candidate_values in values.items()
    }
    fastest = min(candidate_ids, key=lambda item: (medians[item], item))
    comparisons: dict[str, Any] = {}
    singleton = True
    for offset, candidate in enumerate(candidate_ids):
        if candidate == fastest:
            continue
        low, high = _bootstrap_ratio_interval(
            values[candidate],
            values[fastest],
            confidence_level=config.confidence_level,
            draws=config.bootstrap_draws,
            seed=config.bootstrap_seed + offset,
        )
        materially_slower = low > 1.0 + config.practical_tie_fraction
        singleton = singleton and materially_slower
        comparisons[candidate] = {
            "median_ratio_to_fastest": medians[candidate] / medians[fastest],
            "paired_ratio_ci": [low, high],
            "materially_slower": materially_slower,
        }
    drifts = {
        candidate: _half_drift(
            [
                value / reference
                for value, reference in zip(candidate_values, values["fused"], strict=True)
            ]
        )
        for candidate, candidate_values in values.items()
        if candidate != "fused"
    }
    return {
        "query_id": query_id.upper(),
        "block_count": len(blocks),
        "candidate_median_ms": medians,
        "diagnostic_fastest_candidate": fastest,
        "singleton_winner": fastest if singleton else None,
        "comparisons_to_fastest": comparisons,
        "paired_ratio_half_drift": drifts,
        "stability_pass": max(drifts.values(), default=0.0)
        <= config.maximum_paired_ratio_half_drift,
    }


def run_tpch_scale_confirmation(
    project_root: Path,
    config: TpchScaleConfirmationConfig,
    *,
    progress: Callable[[int, int, str, float, float], None] | None = None,
) -> Path:
    """Execute the one-shot balanced SF10 scale confirmation."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for SF10") from exc

    root = project_root.resolve()
    commit, dirty = tpch_git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("SF10 confirmation requires a clean tree")
    prior = root / config.prior_negative_path
    if hashlib.sha256(prior.read_bytes()).hexdigest() != config.prior_negative_sha256:
        raise GovernedRealDataSmokeError("Frozen SF1 negative evidence changed")
    database, artifact = verify_tpch_artifact(root, scale_factor=config.scale_factor)
    snapshot = f"sf10-{str(artifact['sha256'])[:12]}"
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / config.results_dir / run_id
    output.mkdir(parents=True, exist_ok=False)

    connection = duckdb.connect(str(database), read_only=True)
    all_rows: list[TpchAdmissionMeasurement] = []
    plan_evidence: dict[str, Any] = {}
    bound_inputs: dict[str, Any] = {}
    total = len(config.query_targets) * 6 * config.measured_rounds_per_permutation * 3
    completed = 0
    started_all = time.perf_counter()
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        extension = (root / "data/processed/duckdb_extensions").resolve()
        connection.execute(f"SET extension_directory = {_extension_literal(extension)}")
        connection.execute("LOAD tpch")
        # Views are schema-compatible; trusted dataset IDs are supplied by the
        # scale-bound catalog and TableBindings below.
        sf1_bindings = _create_bindings(connection)
        bindings = TableBindings(
            dataset_tables={
                key.replace("tpch_sf1_", "tpch_sf10_"): value
                for key, value in sf1_bindings.dataset_tables.items()
            }
        )

        for query_offset, (query_id, targets) in enumerate(config.query_targets):
            candidates, binding_record = _compiled_candidates(
                root, query_id, targets, snapshot, bindings
            )
            bound_inputs[query_id] = binding_record
            official_row = connection.execute(
                "SELECT query FROM tpch_queries() WHERE query_nr = ?",
                [int(query_id[1:])],
            ).fetchone()
            if official_row is None:
                raise GovernedRealDataSmokeError(
                    f"Official SF10 SQL missing for {query_id.upper()}"
                )
            cursor = connection.execute(str(official_row[0]))
            official_rows = tuple(tuple(row) for row in cursor.fetchall())
            official_columns = tuple(str(item[0]) for item in cursor.description)
            official = materialize_query_result(official_columns, official_rows)

            fingerprints: set[str] = set()
            observed: list[dict[str, Any]] = []
            candidate_ids = tuple(sorted(candidates))
            for candidate_id in candidate_ids:
                compiled = candidates[candidate_id]
                execution = execute_with_connection(compiled, connection)
                if (
                    execution.columns != official.columns
                    or execution.result_digest != official.result_digest
                ):
                    raise GovernedRealDataSmokeError(
                        f"{query_id.upper()} SF10 result differs from official"
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
                        "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                        "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                    }
                )
            if len(fingerprints) != 3:
                raise GovernedRealDataSmokeError(f"{query_id.upper()} SF10 plans collapsed")
            if any(item["peak_temp_directory_bytes"] > 0 for item in observed):
                raise GovernedRealDataSmokeError(f"{query_id.upper()} SF10 preflight spilled")
            plan_evidence[query_id] = {
                "official_result_digest": official.result_digest,
                "official_row_count": official.row_count,
                "candidates": observed,
            }

            for warmup in range(config.warmup_rounds):
                order = candidate_ids[warmup % 3 :] + candidate_ids[: warmup % 3]
                for candidate_id in order:
                    execute_with_connection(candidates[candidate_id], connection)

            permutations = list(itertools.permutations(candidate_ids))
            random.Random(config.order_seed + query_offset).shuffle(permutations)
            block_index = 0
            for repetition in range(config.measured_rounds_per_permutation):
                for permutation_index, order in enumerate(permutations):
                    block_index += 1
                    for position, candidate_id in enumerate(order, start=1):
                        start = time.perf_counter()
                        execution = execute_with_connection(candidates[candidate_id], connection)
                        latency_ms = (time.perf_counter() - start) * 1000.0
                        if execution.result_digest != official.result_digest:
                            raise GovernedRealDataSmokeError(
                                f"{query_id.upper()} SF10 timed result changed"
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
                        if progress and (completed == total or completed % 3 == 0):
                            elapsed = time.perf_counter() - started_all
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
    _atomic_json(output / "bound_inputs.json", bound_inputs)
    analyses = [
        _analyze(
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
        "all_queries_have_singleton_winner": len(winners) == 2,
        "distinct_singleton_winners": len(set(winners))
        >= config.minimum_distinct_singleton_winners,
        "all_results_match_official": True,
        "all_physical_plans_distinct": True,
        "no_spill": True,
    }
    status = (
        "PASS_TPCH_SF10_MULTICANDIDATE_SCALE_CONFIRMATION"
        if all(gates.values())
        else "FAIL_TPCH_SF10_MULTICANDIDATE_SCALE_CONFIRMATION_STOP"
    )
    summary = {
        "schema_version": 1,
        "status": status,
        "source_commit": commit,
        "source_dirty": dirty,
        "artifact_sha256": artifact["sha256"],
        "snapshot": snapshot,
        "config": asdict(config),
        "measurement_count": len(all_rows),
        "elapsed_seconds": time.perf_counter() - started_all,
        "queries": analyses,
        "singleton_winners": winners,
        "gates": gates,
        "plan_evidence": plan_evidence,
        "bound_inputs_digest": _digest(bound_inputs),
        "paper_performance_evidence": False,
        "scientific_boundary": (
            "Scale confirmation after inspecting SF1. Passing establishes "
            "query-dependent SF10 winners but is not an optimizer holdout."
        ),
    }
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_id, "summary": str((output / "summary.json").relative_to(root))},
    )
    return output / "summary.json"
