"""Paired system-overhead measurements for the governed real-data path.

This runner deliberately keeps the already-frozen optimizer out of the timing
loop.  Every layer executes the same compiled, policy-compliant SQL; the only
difference is how much of the TrustAero control path is enabled.  Consequently
the reported deltas measure validation, planning, lineage, and certificate
overhead rather than a change in query semantics.

The first configuration is a small pilot.  It must pass integrity checks before
the formal 500K/full-month matrix is authorized.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
import platform
import random
import statistics
import sys
import time
from collections.abc import Callable
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
from trustaero.execution import (
    CompiledQuery,
    TableBindings,
    capture_source_lineage,
    compile_validated_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.real_data_governed import (
    _certificate_events,
    _create_full_month_views,
    _create_trusted_views,
    _load_json,
)
from trustaero.experiments.real_data_pilot import _semantic_digest
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    GovernedExecutionCertificate,
    PolicySet,
    ValidatedLogicalPlan,
)
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)
from trustaero.validator.service import validate

DatasetName = Literal["bts", "nyc_tlc"]
ScaleName = int | Literal["full_month"]
OrderingDesign = Literal[
    "greedy_position_balance",
    "complete_permutation_cycles",
]
LayerId = Literal[
    "direct_database_equivalent_sql",
    "trustaero_without_lineage_or_certificate",
    "trustaero_with_source_lineage",
    "complete_trustaero_with_certificate",
]

LAYER_IDS: tuple[LayerId, ...] = (
    "direct_database_equivalent_sql",
    "trustaero_without_lineage_or_certificate",
    "trustaero_with_source_lineage",
    "complete_trustaero_with_certificate",
)


class SystemScalabilityError(RuntimeError):
    """Raised when an experiment invariant fails closed."""


@dataclass(frozen=True, slots=True)
class ScalabilityWorkload:
    """One real workload evaluated at predeclared cardinality scales."""

    dataset: DatasetName
    scales: tuple[ScaleName, ...]

    def __post_init__(self) -> None:
        if self.dataset not in {"bts", "nyc_tlc"}:
            raise ValueError(f"Unsupported real workload: {self.dataset}")
        if not self.scales or len(self.scales) != len(set(self.scales)):
            raise ValueError("Scalability scales must be nonempty and unique")
        if any(isinstance(scale, int) and scale < 1 for scale in self.scales):
            raise ValueError("Numeric scales must be positive")


@dataclass(frozen=True, slots=True)
class SystemScalabilityConfig:
    """Immutable paired timing protocol for one pilot or formal run."""

    results_dir: str
    workloads: tuple[ScalabilityWorkload, ...]
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    require_clean_git: bool
    experiment_role: str
    ordering_design: OrderingDesign = "greedy_position_balance"

    def __post_init__(self) -> None:
        if not self.results_dir or Path(self.results_dir).is_absolute():
            raise ValueError("Results directory must be repository-relative")
        if not self.workloads:
            raise ValueError("At least one workload is required")
        if self.warmup_blocks < 1 or self.measured_blocks < 3:
            raise ValueError("Pilot requires >=1 warmup and >=3 measured blocks")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 256:
            raise ValueError("DuckDB resource controls are invalid")
        if self.experiment_role not in {
            "system_scalability_governance_pilot",
            "system_scalability_governance_formal",
            "system_scalability_governance_confirmation",
        }:
            raise ValueError("Unknown system-scalability experiment role")
        if self.ordering_design not in {
            "greedy_position_balance",
            "complete_permutation_cycles",
        }:
            raise ValueError("Unknown layer-ordering design")
        if (
            self.ordering_design == "complete_permutation_cycles"
            and self.measured_blocks % len(tuple(itertools.permutations(LAYER_IDS))) != 0
        ):
            raise ValueError(
                "Complete-permutation ordering requires measured blocks to be a multiple of 24"
            )

    @property
    def unit_count(self) -> int:
        return sum(len(workload.scales) for workload in self.workloads)


@dataclass(frozen=True, slots=True)
class ScalabilityUnit:
    """One dataset-scale pair that can be resumed atomically."""

    dataset: DatasetName
    scale: ScaleName

    @property
    def scale_label(self) -> str:
        return str(self.scale)

    @property
    def unit_id(self) -> str:
        return f"{self.dataset}-{self.scale_label}"


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """Validated and compiled reference used by all equivalent layers."""

    raw_plan: dict[str, Any]
    logical_plan: ValidatedLogicalPlan
    physical_plan: ApprovedPhysicalPlan
    compiled_query: CompiledQuery
    bindings: TableBindings


def load_system_scalability_config(path: str | Path) -> SystemScalabilityConfig:
    """Load a strict configuration without silently accepting extra shapes."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["workloads"] = tuple(
        ScalabilityWorkload(
            dataset=item["dataset"],
            scales=tuple(item["scales"]),
        )
        for item in payload["workloads"]
    )
    return SystemScalabilityConfig(**payload)


def system_scalability_units(
    config: SystemScalabilityConfig,
) -> tuple[ScalabilityUnit, ...]:
    """Expand the declared matrix in deterministic dataset/scale order."""

    return tuple(
        ScalabilityUnit(workload.dataset, scale)
        for workload in config.workloads
        for scale in workload.scales
    )


def semantic_result_digest(
    columns: tuple[str, ...],
    rows: tuple[tuple[Any, ...], ...],
) -> str:
    """Hash a relational bag independently of unspecified output row order.

    SQL results without ``ORDER BY`` have no row-order contract. Sorting the
    canonical row encodings preserves duplicates while preventing a harmless
    aggregate-group ordering change from looking like a semantic mismatch.
    """

    return _semantic_digest(columns, rows)


def balanced_layer_orders(
    count: int,
    *,
    seed: int,
) -> tuple[tuple[LayerId, ...], ...]:
    """Return deterministic orders with position imbalance of at most one.

    Thirty blocks cannot be perfectly balanced across four positions.  The
    greedy construction therefore minimizes the position-count range and
    guarantees the smallest attainable imbalance instead of pretending that
    the schedule is exactly balanced.
    """

    if count < 1:
        raise ValueError("Order count must be positive")
    permutations = list(itertools.permutations(LAYER_IDS))
    # A deterministic rotation changes tie order across experiment units.
    offset = seed % len(permutations)
    permutations = permutations[offset:] + permutations[:offset]
    position_counts = {layer: [0 for _ in LAYER_IDS] for layer in LAYER_IDS}
    chosen: list[tuple[LayerId, ...]] = []
    for _ in range(count):
        best: tuple[LayerId, ...] | None = None
        best_score: tuple[int, int, int] | None = None
        for candidate_index, candidate in enumerate(permutations):
            projected = {layer: counts.copy() for layer, counts in position_counts.items()}
            for position, layer in enumerate(candidate):
                projected[layer][position] += 1
            ranges = [max(counts) - min(counts) for counts in projected.values()]
            score = (max(ranges), sum(ranges), candidate_index)
            if best_score is None or score < best_score:
                best_score = score
                best = candidate
        assert best is not None
        chosen.append(best)
        for position, layer in enumerate(best):
            position_counts[layer][position] += 1
        # Avoid repeatedly choosing the same permutation when several schedules
        # have equal balance quality.
        permutations = permutations[1:] + permutations[:1]
    return tuple(chosen)


def complete_permutation_layer_orders(
    count: int,
    *,
    seed: int,
) -> tuple[tuple[LayerId, ...], ...]:
    """Repeat every four-layer permutation equally often.

    A complete cycle contains all 24 possible orders.  It therefore balances
    both execution position and immediate predecessor effects exactly.  The
    deterministic interleaving varies the temporal placement of each
    permutation without changing that guarantee.
    """

    permutations = list(itertools.permutations(LAYER_IDS))
    if count < 1 or count % len(permutations) != 0:
        raise ValueError("Complete-permutation order count must be a multiple of 24")

    def maximum_position_run(orders: list[tuple[LayerId, ...]]) -> int:
        maximum = 0
        for layer in LAYER_IDS:
            previous: int | None = None
            run = 0
            for order in orders:
                position = order.index(layer)
                run = run + 1 if position == previous else 1
                previous = position
                maximum = max(maximum, run)
        return maximum

    # Search deterministically for a well-interleaved ordering.  Every attempt
    # still contains each permutation equally often, while the run constraint
    # prevents one position from being concentrated in a short machine phase.
    for attempt in range(1_000):
        orders: list[tuple[LayerId, ...]] = []
        for cycle in range(count // len(permutations)):
            cycle_orders = permutations.copy()
            random.Random(seed + attempt * 1_000_003 + cycle * 104_729).shuffle(cycle_orders)
            orders.extend(cycle_orders)
        if maximum_position_run(orders) <= 3:
            return tuple(orders)
    raise RuntimeError("Could not construct a sufficiently interleaved permutation schedule")


def measurement_layer_orders(
    count: int,
    *,
    seed: int,
    design: OrderingDesign,
) -> tuple[tuple[LayerId, ...], ...]:
    """Select the explicitly configured measurement-order design."""

    if design == "complete_permutation_cycles":
        return complete_permutation_layer_orders(count, seed=seed)
    return balanced_layer_orders(count, seed=seed)


def _scale_input_rows(connection: Any, dataset: DatasetName) -> int:
    table = "trust_bts_flights" if dataset == "bts" else "trust_nyc_trips"
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    if row is None:
        raise SystemScalabilityError(f"Could not count input rows for {dataset}")
    return int(row[0])


def _raw_plan(examples: Path, dataset: DatasetName) -> dict[str, Any]:
    filename = "bts_governed_read.json" if dataset == "bts" else "nyc_governed_aggregate.json"
    return _load_json(examples / "plans" / filename)


def _prepare_execution(
    raw_plan: dict[str, Any],
    policy: PolicySet,
    catalog: InMemoryCatalog,
    bindings: TableBindings,
) -> PreparedExecution:
    """Build the reference execution once outside measured samples."""

    response = validate(raw_plan, policy, catalog)
    if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
        codes = [item.code.value for item in response.diagnostics]
        raise SystemScalabilityError(f"Reference plan was not approved: {codes}")
    logical = response.validated_plan
    if logical is None:
        raise SystemScalabilityError("Reference validation returned no logical plan")
    compiled = compile_validated_plan(logical, catalog, bindings)
    physical = plan_physical_execution(logical, backend="duckdb")
    if physical.unimplemented_backend_features:
        raise SystemScalabilityError(
            f"Backend fragment is incomplete: {physical.unimplemented_backend_features}"
        )
    return PreparedExecution(raw_plan, logical, physical, compiled, bindings)


def _build_certificate(
    logical: ValidatedLogicalPlan,
    physical: ApprovedPhysicalPlan,
    *,
    execution_id: str,
    result_digest: str,
    lineage: Any,
) -> GovernedExecutionCertificate:
    """Bind independently observed result and lineage evidence."""

    if lineage.evidence is None or lineage.lineage_digest is None:
        raise SystemScalabilityError("Complete layer requires source-lineage evidence")
    return GovernedExecutionCertificate(
        certificate_id=f"cert-{execution_id}",
        task_digest=logical.validation.canonical_digest,
        logical_plan_id=logical.logical_plan_id,
        physical_plan_id=physical.physical_plan_id,
        policy_snapshot=logical.bindings.policy_snapshot,
        data_snapshots=logical.bindings.data_snapshots,
        events=_certificate_events(
            physical,
            policy_snapshot=logical.bindings.policy_snapshot,
            result_digest=result_digest,
            lineage_digest=lineage.lineage_digest,
        ),
        result_digest=result_digest,
        lineage_evidence=lineage.evidence,
        lineage_digest=lineage.lineage_digest,
    )


def _validate_compile_plan(
    prepared: PreparedExecution,
    policy: PolicySet,
    catalog: InMemoryCatalog,
) -> tuple[ValidatedLogicalPlan, ApprovedPhysicalPlan, CompiledQuery, float, float]:
    """Repeat the public control path and expose its two main timing parts."""

    validation_started = time.perf_counter_ns()
    response = validate(prepared.raw_plan, policy, catalog)
    validation_ms = (time.perf_counter_ns() - validation_started) / 1_000_000
    logical = response.validated_plan
    if (
        response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}
        or logical is None
    ):
        raise SystemScalabilityError("Timed validation diverged from preflight")

    planning_started = time.perf_counter_ns()
    compiled = compile_validated_plan(
        logical,
        catalog,
        prepared.bindings,
    )
    physical = plan_physical_execution(logical, backend="duckdb")
    planning_ms = (time.perf_counter_ns() - planning_started) / 1_000_000
    if (
        logical.validation.canonical_digest != prepared.logical_plan.validation.canonical_digest
        or compiled.sql != prepared.compiled_query.sql
        or compiled.parameters != prepared.compiled_query.parameters
    ):
        raise SystemScalabilityError("Timed control path changed the executable query")
    return logical, physical, compiled, validation_ms, planning_ms


def execute_measurement_layer(
    connection: Any,
    prepared: PreparedExecution,
    policy: PolicySet,
    catalog: InMemoryCatalog,
    *,
    layer_id: LayerId,
    execution_id: str,
) -> dict[str, Any]:
    """Execute one layer and return a fully decomposed timing observation."""

    end_to_end_started = time.perf_counter_ns()
    # None means that a component is outside this layer. A numeric zero would
    # incorrectly look like an observed, cost-free operation in paper tables.
    validation_ms: float | None = None
    planning_ms: float | None = None
    lineage_ms: float | None = None
    certificate_ms: float | None = None
    lineage_digest: str | None = None
    lineage_edge_digest: str | None = None
    certificate_status: str | None = None

    if layer_id == "direct_database_equivalent_sql":
        logical = prepared.logical_plan
        physical = prepared.physical_plan
        compiled = prepared.compiled_query
    else:
        logical, physical, compiled, validation_ms, planning_ms = _validate_compile_plan(
            prepared, policy, catalog
        )

    database_started = time.perf_counter_ns()
    execution = execute_with_connection(compiled, connection)
    database_ms = (time.perf_counter_ns() - database_started) / 1_000_000

    lineage = None
    if layer_id in {
        "trustaero_with_source_lineage",
        "complete_trustaero_with_certificate",
    }:
        lineage = capture_source_lineage(
            logical,
            execution_id=execution_id,
            result_id=execution.result_digest,
        )
        lineage_ms = lineage.latency_ms
        lineage_digest = lineage.lineage_digest
        if lineage.evidence is None:
            raise SystemScalabilityError("Lineage-enabled layer emitted no evidence")
        lineage_edge_digest = lineage.evidence.edge_digest

    if layer_id == "complete_trustaero_with_certificate":
        certificate = _build_certificate(
            logical,
            physical,
            execution_id=execution_id,
            result_digest=execution.result_digest,
            lineage=lineage,
        )
        verification_started = time.perf_counter_ns()
        verification = verify_execution_certificate(
            logical,
            physical,
            certificate,
            observed_result_digest=execution.result_digest,
        )
        certificate_ms = (time.perf_counter_ns() - verification_started) / 1_000_000
        certificate_status = verification.status.value
        if verification.status != CertificateVerificationStatus.PARTIAL:
            codes = [item.code.value for item in verification.diagnostics]
            raise SystemScalabilityError(f"Certificate verification failed closed: {codes}")

    end_to_end_ms = (time.perf_counter_ns() - end_to_end_started) / 1_000_000
    semantic_digest = semantic_result_digest(execution.columns, execution.rows)
    return {
        "layer_id": layer_id,
        "policy_validation_latency_ms": validation_ms,
        "planner_latency_ms": planning_ms,
        "database_execution_latency_ms": database_ms,
        "lineage_capture_latency_ms": lineage_ms,
        "certificate_verification_latency_ms": certificate_ms,
        "end_to_end_latency_ms": end_to_end_ms,
        "output_rows": execution.row_count,
        "result_digest": semantic_digest,
        # Retain the executor's order-sensitive binding for diagnosis. It is
        # used inside the certificate for this execution, but not for cross-run
        # relational equivalence.
        "executor_result_digest": execution.result_digest,
        "lineage_digest": lineage_digest,
        # The instance digest includes execution_id and should differ between
        # runs. The edge digest is the stable semantic value for equivalence.
        "lineage_edge_digest": lineage_edge_digest,
        "certificate_status": certificate_status,
        # The lineageless layer is an explicit cost ablation.  It is not
        # represented as a fully satisfied governed execution.
        "obligation_state": (
            "ablation_pending_lineage"
            if layer_id == "trustaero_without_lineage_or_certificate"
            else "not_applicable"
            if layer_id == "direct_database_equivalent_sql"
            else "lineage_observed"
            if layer_id == "trustaero_with_source_lineage"
            else "certificate_partially_verified"
        ),
    }


def _directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999) - 1))
    return ordered[index]


def _optional_median(rows: list[dict[str, Any]], field: str) -> float | None:
    """Return the median of applicable observations, preserving N/A as None."""

    values = [float(row[field]) for row in rows if row[field] is not None]
    return statistics.median(values) if values else None


def summarize_scalability_measurements(
    measurements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize each unit/layer without pooling unrelated scales."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in measurements:
        grouped.setdefault((str(row["unit_id"]), str(row["layer_id"])), []).append(row)
    summaries: list[dict[str, Any]] = []
    for (unit_id, layer_id), rows in sorted(grouped.items()):
        end_to_end = [float(row["end_to_end_latency_ms"]) for row in rows]
        summaries.append(
            {
                "unit_id": unit_id,
                "layer_id": layer_id,
                "runs": len(rows),
                "median_end_to_end_latency_ms": statistics.median(end_to_end),
                "p95_end_to_end_latency_ms": _percentile_95(end_to_end),
                "median_policy_validation_latency_ms": _optional_median(
                    rows, "policy_validation_latency_ms"
                ),
                "median_planner_latency_ms": _optional_median(rows, "planner_latency_ms"),
                "median_database_execution_latency_ms": _optional_median(
                    rows, "database_execution_latency_ms"
                ),
                "median_lineage_capture_latency_ms": _optional_median(
                    rows, "lineage_capture_latency_ms"
                ),
                "median_certificate_verification_latency_ms": _optional_median(
                    rows, "certificate_verification_latency_ms"
                ),
                "output_rows": int(rows[0]["output_rows"]),
            }
        )
    return {
        "status": "PASS_SYSTEM_SCALABILITY_MEASUREMENT_INTEGRITY",
        "measurement_count": len(measurements),
        "unit_count": len({str(row["unit_id"]) for row in measurements}),
        "layer_summaries": summaries,
    }


def _write_measurements(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _run_unit(
    config: SystemScalabilityConfig,
    unit: ScalabilityUnit,
    *,
    root: Path,
    policy: PolicySet,
    catalog: InMemoryCatalog,
    progress: Callable[[int, int, str, float], None] | None,
    done_before: int,
    total_blocks: int,
    started: float,
    spill_run_id: str,
) -> dict[str, Any]:
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        # A run-specific directory prevents stale spill files from an aborted
        # experiment inflating this run's footprint.
        spill = root / "data/tmp/duckdb" / f"system-scalability-{spill_run_id}-{unit.unit_id}"
        spill.mkdir(parents=True, exist_ok=True)
        connection.execute("SET temp_directory = '" + str(spill).replace("'", "''") + "'")
        if unit.scale == "full_month":
            artifacts = verify_real_data_full_month_artifacts(
                root / "data",
                unit.dataset,
            )
            bindings = _create_full_month_views(
                connection,
                root / "data",
                workload=unit.dataset,
            )
        else:
            artifacts = verify_real_data_slice_artifacts(
                root / "data",
                int(unit.scale),
            )
            bindings = _create_trusted_views(
                connection,
                root / "data",
                sample_rows=int(unit.scale),
            )

        examples = root / "examples/real_data"
        prepared = _prepare_execution(
            _raw_plan(examples, unit.dataset),
            policy,
            catalog,
            bindings,
        )
        input_rows = _scale_input_rows(connection, unit.dataset)
        preflight_execution = execute_with_connection(
            prepared.compiled_query,
            connection,
        )
        preflight_semantic_digest = semantic_result_digest(
            preflight_execution.columns,
            preflight_execution.rows,
        )
        observation = observe_duckdb_plan(
            connection,
            prepared.compiled_query.sql,
            prepared.compiled_query.parameters,
            analyze=True,
        )
        profile = {
            "input_rows": input_rows,
            "output_rows": preflight_execution.row_count,
            "result_digest": preflight_semantic_digest,
            "max_intermediate_rows": observation.max_intermediate_cardinality,
            "peak_memory_bytes": observation.peak_buffer_memory_bytes,
            "peak_memory_scope": "duckdb_profile_peak_buffer_memory",
            "temporary_spill_bytes": observation.peak_temp_directory_bytes,
            "profile_latency_is_primary_measurement": False,
            "duckdb_plan_fingerprint": observation.fingerprint,
        }

        for block_index, order in enumerate(
            balanced_layer_orders(config.warmup_blocks, seed=config.order_seed)
        ):
            for position, layer_id in enumerate(order):
                result = execute_measurement_layer(
                    connection,
                    prepared,
                    policy,
                    catalog,
                    layer_id=layer_id,
                    execution_id=(f"warmup-{unit.unit_id}-{block_index}-{position}"),
                )
                if result["result_digest"] != preflight_semantic_digest:
                    raise SystemScalabilityError(f"Warmup result changed: {unit.unit_id}")

        measurements: list[dict[str, Any]] = []
        orders = measurement_layer_orders(
            config.measured_blocks,
            seed=config.order_seed + 1,
            design=config.ordering_design,
        )
        for block_index, order in enumerate(orders):
            block: list[dict[str, Any]] = []
            for position, layer_id in enumerate(order):
                result = execute_measurement_layer(
                    connection,
                    prepared,
                    policy,
                    catalog,
                    layer_id=layer_id,
                    execution_id=f"measured-{unit.unit_id}-{block_index}-{position}",
                )
                block.append(
                    {
                        "unit_id": unit.unit_id,
                        "dataset": unit.dataset,
                        "scale": unit.scale_label,
                        "input_rows": input_rows,
                        "block_index": block_index,
                        "order_position": position,
                        "order_id": "->".join(order),
                        **result,
                    }
                )
            if len({str(row["result_digest"]) for row in block}) != 1:
                raise SystemScalabilityError(f"Equivalent layers changed results: {unit.unit_id}")
            lineage_edge_digests = {
                str(row["lineage_edge_digest"])
                for row in block
                if row["lineage_edge_digest"] is not None
            }
            if len(lineage_edge_digests) != 1:
                raise SystemScalabilityError(f"Lineage-enabled layers disagree: {unit.unit_id}")
            measurements.extend(block)
            if progress is not None:
                progress(
                    done_before + block_index + 1,
                    total_blocks,
                    f"{unit.unit_id} block={block_index + 1}",
                    time.perf_counter() - started,
                )

        return {
            "unit": asdict(unit),
            "artifact_bindings": [asdict(item) for item in artifacts],
            "profile": profile,
            "spill_directory_bytes_after_unit": _directory_bytes(spill),
            "measurements": measurements,
        }
    finally:
        connection.close()


def _environment(
    config: SystemScalabilityConfig,
    *,
    commit: str,
    dirty: bool,
) -> dict[str, Any]:
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
        "cache_regime": "warm_same_unit_connection",
    }


def run_system_scalability(
    config: SystemScalabilityConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume one unit at a time with atomic checkpoints."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise SystemScalabilityError("System scalability timing requires a clean Git commit")
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    output_root = root / config.results_dir
    if resume_run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = output_root / run_id
        (output / "units").mkdir(parents=True)
        _atomic_json(output / "config.json", asdict(config))
        _atomic_json(
            output / "environment.json",
            {
                **_environment(config, commit=commit, dirty=dirty),
                "config_path": str(config_path.resolve()),
            },
        )
        _atomic_json(output_root / "latest_run.json", {"run_id": run_id})
    else:
        output = output_root / resume_run_id
        frozen = json.loads((output / "config.json").read_text(encoding="utf-8"))
        if frozen != json.loads(json.dumps(asdict(config))):
            raise SystemScalabilityError("Resume configuration changed")
        environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))
        if environment.get("commit_hash") != commit:
            raise SystemScalabilityError(
                "Resume commit changed; start a new run to avoid mixed-code timings"
            )

    units = system_scalability_units(config)
    total_blocks = len(units) * config.measured_blocks
    started = time.perf_counter()
    completed = 0
    for unit in units:
        unit_path = output / "units" / f"{unit.unit_id}.json"
        if unit_path.is_file():
            completed += config.measured_blocks
            continue
        try:
            payload = _run_unit(
                config,
                unit,
                root=root,
                policy=policy,
                catalog=catalog,
                progress=progress,
                done_before=completed,
                total_blocks=total_blocks,
                started=started,
                spill_run_id=output.name,
            )
        except Exception as error:
            # Retain the failed unit and exact source commit. Partial timing is
            # diagnostic only and is never included in the final summary.
            _atomic_json(
                output / "failures" / f"{unit.unit_id}.json",
                {
                    "status": "FAILED_UNIT_RETAINED",
                    "unit": asdict(unit),
                    "commit_hash": commit,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            raise
        _atomic_json(unit_path, payload)
        completed += config.measured_blocks

    measurements: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    for unit in units:
        payload = json.loads(
            (output / "units" / f"{unit.unit_id}.json").read_text(encoding="utf-8")
        )
        measurements.extend(payload["measurements"])
        profiles.append({"unit_id": unit.unit_id, **payload["profile"]})
    _write_measurements(output / "measurements.csv", measurements)
    summary = summarize_scalability_measurements(measurements)
    summary.update(
        {
            "experiment_role": config.experiment_role,
            # Measurement integrity alone never authorizes a paper claim. A
            # separate frozen evaluator must pass confidence/stability gates.
            "paper_performance_evidence": False,
            "formal_evaluation_required": True,
            "profiles": profiles,
            "all_equivalent_results": True,
            "all_lineage_enabled_layers_equivalent": True,
            "optimizer_refit_or_retune_performed": False,
        }
    )
    _atomic_json(output / "summary.json", summary)
    return output
