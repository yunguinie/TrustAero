"""Phase 1 DuckDB-backed minimal execution experiment runner.

Phase 1 turns the earlier smoke script into a repeatable artifact. It measures a
single trusted path: validated logical plan -> parameterized SQL -> DuckDB
materialization -> result digest -> certificate verification.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import (
    TableBindings,
    capture_source_lineage,
    compile_validated_plan,
    execute_with_connection,
)
from trustaero.experiments.loader import load_json
from trustaero.experiments.models import Phase1Config, Phase1ExecutionResult
from trustaero.ir.enums import LineageLevel, ObligationType, ValidationStatus
from trustaero.ir.models import (
    ExecutionEvent,
    GovernedExecutionCertificate,
    LineageEvidenceSummary,
    PolicySet,
)
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.certificate import verify_execution_certificate
from trustaero.validator.service import validate

EventType = Literal[
    "PlanApproved",
    "OperatorStarted",
    "OperatorCompleted",
    "PolicyDecisionRecorded",
    "ResultMaterialized",
    "LineageRecorded",
    "CertificateEmitted",
]


@dataclass(frozen=True)
class Phase1Case:
    """One deterministic real-execution case in the minimal Phase 1 matrix."""

    case_id: str
    case_category: str
    scenario: str
    raw_plan: dict[str, Any]
    expected_row_count: int
    expected_rows: tuple[tuple[Any, ...], ...]
    source_lineage_required: bool = False


def _repo_root() -> Path:
    """Return the repository root from this source file location."""

    return Path(__file__).resolve().parents[3]


def _git_commit(root: Path) -> str:
    """Record the exact source revision when git metadata is available."""

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


def _run_id() -> str:
    """Use a sortable UTC run ID so result folders are easy to compare."""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _percentile_95(values: list[float]) -> float:
    """Return a nearest-rank P95 for short repeated DuckDB measurements."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def _event(
    sequence: int,
    event_type: EventType,
    payload_digest: str,
    operator_id: str | None = None,
) -> ExecutionEvent:
    """Build a minimal trusted-executor event for the Phase 1 certificate."""

    return ExecutionEvent(
        sequence=sequence,
        event_type=event_type,
        operator_id=operator_id,
        payload_digest=payload_digest,
    )


def _prepare_duckdb_table(connection: Any) -> None:
    """Create deterministic in-memory data for the current Phase 1 case.

    The data is deliberately tiny because this phase checks wiring and digest
    binding, not DBMS scalability. Larger selectivity experiments come later.
    """

    connection.execute(
        """
        CREATE OR REPLACE TABLE earthquake_events(
            event_id VARCHAR,
            event_time TIMESTAMPTZ,
            latitude DOUBLE,
            longitude DOUBLE,
            magnitude DOUBLE
        )
        """
    )
    connection.execute(
        """
        CREATE OR REPLACE TABLE score_lookup(
            score_key DOUBLE,
            severity_label VARCHAR
        )
        """
    )
    connection.execute("INSERT INTO score_lookup VALUES (4.8, 'moderate'), (5.1, 'strong')")
    connection.execute(
        """
        INSERT INTO earthquake_events VALUES
          ('eq-001', TIMESTAMPTZ '2026-06-01 00:00:00+00:00', 39.9, 116.4, 4.8),
          ('eq-002', TIMESTAMPTZ '2026-06-02 00:00:00+00:00', 40.1, 116.2, 5.1)
        """
    )


def _grid_center(value: float, offset: float, precision_km: float) -> float:
    """Mirror the frozen fixed-grid formula for expected Phase 1 rows."""

    step = precision_km / 111.045
    return math.floor((value + offset) / step) * step - offset + step / 2.0


def _rows_match(
    actual: tuple[tuple[Any, ...], ...],
    expected: tuple[tuple[Any, ...], ...],
) -> bool:
    """Compare deterministic rows while tolerating harmless float roundoff."""

    if len(actual) != len(expected):
        return False
    for actual_row, expected_row in zip(actual, expected, strict=True):
        if len(actual_row) != len(expected_row):
            return False
        for actual_value, expected_value in zip(actual_row, expected_row, strict=True):
            if isinstance(actual_value, float) and isinstance(expected_value, float):
                if not math.isclose(actual_value, expected_value, rel_tol=1e-12, abs_tol=1e-12):
                    return False
            elif actual_value != expected_value:
                return False
    return True


def _phase1_cases(baseline_plan: dict[str, Any]) -> tuple[Phase1Case, ...]:
    """Build the fixed Phase 1 execution matrix from the accepted baseline plan.

    Each case stays inside the executable V1 fragment. The goal is to confirm
    that supported relational/spatio-temporal operators survive validation,
    SQL compilation, DuckDB execution, and certificate digest binding.
    """

    baseline = copy.deepcopy(baseline_plan)

    magnitude_filter = copy.deepcopy(baseline_plan)
    magnitude_filter["plan_id"] = "p1-filter-magnitude"
    magnitude_filter["operators"].insert(
        1,
        {
            "operator_type": "Filter",
            "operator_id": "op-filter-magnitude",
            "inputs": ["op1"],
            "expression": {
                "expression_type": "comparison",
                "operator": "ge",
                "left": {"expression_type": "field", "field": "magnitude"},
                "right": {
                    "expression_type": "literal",
                    "data_type": "float",
                    "value": 5.0,
                },
            },
        },
    )
    magnitude_filter["operators"][2]["inputs"] = ["op-filter-magnitude"]

    temporal_filter = copy.deepcopy(baseline_plan)
    temporal_filter["plan_id"] = "p1-temporal-window"
    temporal_filter["operators"].insert(
        1,
        {
            "operator_type": "TemporalFilter",
            "operator_id": "op-temporal-window",
            "inputs": ["op1"],
            "field": "event_time",
            "start": "2026-06-01T00:00:00+00:00",
            "end": "2026-06-02T00:00:00+00:00",
        },
    )
    temporal_filter["operators"][2]["inputs"] = ["op-temporal-window"]

    spatial_filter = copy.deepcopy(baseline_plan)
    spatial_filter["plan_id"] = "p1-spatial-radius"
    spatial_filter["operators"].insert(
        1,
        {
            "operator_type": "SpatialFilter",
            "operator_id": "op-spatial-radius",
            "inputs": ["op1"],
            "center": [40.0, 116.3],
            "radius_km": 20.0,
            "crs": "EPSG:4326",
        },
    )
    spatial_filter["operators"][2]["inputs"] = ["op-spatial-radius"]

    join_plan = copy.deepcopy(baseline_plan)
    join_plan["plan_id"] = "p1-join-severity"
    join_plan["request_context"]["action"] = "join"
    join_plan["requested_output"]["fields"] = ["event_id", "severity_label"]
    join_plan["operators"] = [
        join_plan["operators"][0],
        {
            "operator_type": "Project",
            "operator_id": "op-left-project",
            "inputs": ["op1"],
            "fields": ["event_id", "magnitude"],
        },
        {
            "operator_type": "ScanSource",
            "operator_id": "op-score-scan",
            "inputs": [],
            "dataset": "earthquake_scores",
            "snapshot": None,
        },
        {
            "operator_type": "Join",
            "operator_id": "op-score-join",
            "inputs": ["op-left-project", "op-score-scan"],
            "left_field": "magnitude",
            "right_field": "score_key",
            "join_type": "inner",
        },
        {
            "operator_type": "Project",
            "operator_id": "op-join-output",
            "inputs": ["op-score-join"],
            "fields": ["event_id", "severity_label"],
        },
    ]
    join_plan["output_operator"] = "op-join-output"

    aggregate_plan = copy.deepcopy(baseline_plan)
    aggregate_plan["plan_id"] = "p1-aggregate-summary"
    aggregate_plan["request_context"]["action"] = "aggregate"
    aggregate_plan["requested_output"]["fields"] = ["event_count", "mean_magnitude"]
    aggregate_plan["operators"] = [
        aggregate_plan["operators"][0],
        {
            "operator_type": "Aggregate",
            "operator_id": "op-aggregate-output",
            "inputs": ["op1"],
            "group_by": [],
            "aggregates": [
                {
                    "function": "count",
                    "input_field": None,
                    "output_field": "event_count",
                },
                {
                    "function": "avg",
                    "input_field": "magnitude",
                    "output_field": "mean_magnitude",
                },
            ],
        },
    ]
    aggregate_plan["output_operator"] = "op-aggregate-output"

    def mask_plan(method: str) -> dict[str, Any]:
        plan = copy.deepcopy(baseline_plan)
        plan["plan_id"] = f"p1-mask-{method}"
        plan["operators"].insert(
            1,
            {
                "operator_type": "Mask",
                "operator_id": f"op-mask-{method}",
                "inputs": ["op1"],
                "fields": ["event_id"],
                "method": method,
            },
        )
        plan["operators"][2]["inputs"] = [f"op-mask-{method}"]
        return plan

    generalize_plan = copy.deepcopy(baseline_plan)
    generalize_plan["plan_id"] = "p1-generalize-grid"
    generalize_plan["requested_output"]["fields"] = ["latitude", "longitude"]
    generalize_plan["operators"] = [
        generalize_plan["operators"][0],
        {
            "operator_type": "GeneralizeLocation",
            "operator_id": "op-generalize-grid",
            "inputs": ["op1"],
            "fields": ["latitude", "longitude"],
            "precision_km": 5.0,
            "method": "fixed_grid",
            "preserves_selection": True,
        },
        {
            "operator_type": "Project",
            "operator_id": "op-grid-output",
            "inputs": ["op-generalize-grid"],
            "fields": ["latitude", "longitude"],
        },
    ]
    generalize_plan["output_operator"] = "op-grid-output"

    source_lineage_plan = copy.deepcopy(baseline_plan)
    source_lineage_plan["plan_id"] = "p1-source-lineage"

    return (
        Phase1Case(
            case_id="P1-001",
            case_category="project",
            scenario="baseline_project",
            raw_plan=baseline,
            expected_row_count=2,
            expected_rows=(("eq-001", 4.8), ("eq-002", 5.1)),
        ),
        Phase1Case(
            case_id="P1-002",
            case_category="filter",
            scenario="magnitude_ge_5",
            raw_plan=magnitude_filter,
            expected_row_count=1,
            expected_rows=(("eq-002", 5.1),),
        ),
        Phase1Case(
            case_id="P1-003",
            case_category="temporal_filter",
            scenario="first_day_window",
            raw_plan=temporal_filter,
            expected_row_count=1,
            expected_rows=(("eq-001", 4.8),),
        ),
        Phase1Case(
            case_id="P1-004",
            case_category="spatial_filter",
            scenario="radius_20km",
            raw_plan=spatial_filter,
            expected_row_count=2,
            expected_rows=(("eq-001", 4.8), ("eq-002", 5.1)),
        ),
        Phase1Case(
            case_id="P1-005",
            case_category="join",
            scenario="magnitude_to_severity",
            raw_plan=join_plan,
            expected_row_count=2,
            expected_rows=(("eq-001", "moderate"), ("eq-002", "strong")),
        ),
        Phase1Case(
            case_id="P1-006",
            case_category="aggregate",
            scenario="count_and_average",
            raw_plan=aggregate_plan,
            expected_row_count=1,
            expected_rows=((2, 4.95),),
        ),
        Phase1Case(
            case_id="P1-007",
            case_category="mask_redact",
            scenario="redact_identifier",
            raw_plan=mask_plan("redact"),
            expected_row_count=2,
            expected_rows=(("[REDACTED]", 4.8), ("[REDACTED]", 5.1)),
        ),
        Phase1Case(
            case_id="P1-008",
            case_category="mask_hash",
            scenario="sha256_identifier",
            raw_plan=mask_plan("hash"),
            expected_row_count=2,
            expected_rows=(
                (hashlib.sha256(b"eq-001").hexdigest(), 4.8),
                (hashlib.sha256(b"eq-002").hexdigest(), 5.1),
            ),
        ),
        Phase1Case(
            case_id="P1-009",
            case_category="mask_null",
            scenario="null_identifier",
            raw_plan=mask_plan("null"),
            expected_row_count=2,
            expected_rows=((None, 4.8), (None, 5.1)),
        ),
        Phase1Case(
            case_id="P1-010",
            case_category="generalize_location",
            scenario="fixed_grid_5km",
            raw_plan=generalize_plan,
            expected_row_count=2,
            expected_rows=(
                (_grid_center(39.9, 90.0, 5.0), _grid_center(116.4, 180.0, 5.0)),
                (_grid_center(40.1, 90.0, 5.0), _grid_center(116.2, 180.0, 5.0)),
            ),
        ),
        Phase1Case(
            case_id="P1-011",
            case_category="source_lineage",
            scenario="source_snapshot_evidence",
            raw_plan=source_lineage_plan,
            expected_row_count=2,
            expected_rows=(("eq-001", 4.8), ("eq-002", 5.1)),
            source_lineage_required=True,
        ),
    )


def _policy_for_case(policy: PolicySet, case: Phase1Case) -> PolicySet:
    """Add source lineage only to the dedicated instrumentation case."""

    if not case.source_lineage_required:
        return policy
    raw = policy.model_dump(mode="json")
    earthquake_rule = next(rule for rule in raw["rules"] if rule["policy_id"] == "P-RESEARCH-EQ")
    earthquake_rule["obligations"] = [
        {"obligation_type": "LINEAGE_CAPTURE", "parameters": {"level": "source"}}
    ]
    return PolicySet.model_validate(raw)


def _build_certificate(
    plan_digest: str,
    logical_plan_id: str,
    physical_plan_id: str,
    policy_snapshot: str,
    data_snapshots: dict[str, str],
    physical_operators: tuple[Any, ...],
    result_digest: str,
    lineage_evidence: LineageEvidenceSummary | None = None,
    lineage_digest: str | None = None,
) -> GovernedExecutionCertificate:
    """Create a minimal certificate whose result digest comes from DuckDB output."""

    events: list[ExecutionEvent] = [_event(0, "PlanApproved", physical_plan_id)]
    sequence = 1
    for operator in physical_operators:
        events.append(
            _event(
                sequence,
                "OperatorStarted",
                f"sha256:start-{operator.physical_operator_id}",
                operator.physical_operator_id,
            )
        )
        sequence += 1
        events.append(
            _event(
                sequence,
                "OperatorCompleted",
                f"sha256:done-{operator.physical_operator_id}",
                operator.physical_operator_id,
            )
        )
        sequence += 1
    events.append(_event(sequence, "ResultMaterialized", result_digest))
    sequence += 1
    if lineage_evidence is not None and lineage_digest is not None:
        events.append(_event(sequence, "LineageRecorded", lineage_digest))
        sequence += 1
    events.append(_event(sequence, "CertificateEmitted", "sha256:certificate"))

    return GovernedExecutionCertificate(
        certificate_id="cert-phase1-duckdb-smoke",
        task_digest=plan_digest,
        logical_plan_id=logical_plan_id,
        physical_plan_id=physical_plan_id,
        policy_snapshot=policy_snapshot,
        data_snapshots=data_snapshots,
        events=tuple(events),
        result_digest=result_digest,
        lineage_evidence=lineage_evidence,
        lineage_digest=lineage_digest,
    )


def _write_csv(path: Path, rows: tuple[Phase1ExecutionResult, ...]) -> None:
    """Write Phase 1 rows with tuple fields flattened for spreadsheet tools."""

    fieldnames = list(Phase1ExecutionResult.__annotations__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in rows:
            row = asdict(result)
            row["unverified_components"] = "|".join(result.unverified_components)
            writer.writerow(row)


def _environment(root: Path, commit_hash: str) -> dict[str, Any]:
    """Capture enough environment detail to reproduce the smoke run."""

    packages = {}
    for package in ("trustaero", "pydantic", "duckdb"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "unknown"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "commit_hash": commit_hash,
        "packages": packages,
        "repo_root": str(root),
    }


def run_phase1(config: Phase1Config) -> Path:
    """Run the minimal DuckDB execution case and write repeatable artifacts."""

    root = _repo_root()
    commit_hash = _git_commit(root)
    run_id = _run_id()
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "failures").mkdir(exist_ok=True)

    import duckdb  # Imported here so core TrustAero remains usable without DuckDB.

    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(load_json(root / "examples/catalogs/minimal_catalog.json"))
    )
    policy = PolicySet.model_validate(load_json(root / "examples/policies/research_policy.json"))
    baseline_plan = load_json(root / "examples/plans/accept_earthquakes.json")
    table_bindings = TableBindings(
        dataset_tables={
            "earthquakes": "earthquake_events",
            "earthquake_scores": "score_lookup",
        }
    )
    rows: list[Phase1ExecutionResult] = []

    for case in _phase1_cases(baseline_plan):
        response = validate(case.raw_plan, _policy_for_case(policy, case), catalog)
        if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
            raise RuntimeError(f"{case.case_id} did not validate: {response.status}")
        if response.validated_plan is None:
            raise RuntimeError(f"{case.case_id} returned no ValidatedLogicalPlan.")
        validated_plan = response.validated_plan

        compiled = compile_validated_plan(validated_plan, catalog, table_bindings)
        physical = plan_physical_execution(validated_plan, backend="duckdb")
        if physical.unimplemented_backend_features:
            raise RuntimeError(
                f"{case.case_id} has no complete DuckDB binding: "
                f"{physical.unimplemented_backend_features}"
            )

        def execute_once(
            compiled_query: Any = compiled,
            case_plan: Any = validated_plan,
            case_physical: Any = physical,
            case_id: str = case.case_id,
        ) -> tuple[float, Any, Any, Any]:
            """Run one isolated in-memory DuckDB execution and measure wall time."""

            connection = duckdb.connect(":memory:")
            try:
                _prepare_duckdb_table(connection)
                started = time.perf_counter()
                execution_result = execute_with_connection(compiled_query, connection)
                latency_ms = (time.perf_counter() - started) * 1000.0
                lineage_capture = capture_source_lineage(
                    case_plan,
                    execution_id=f"{run_id}:{case_id}",
                    result_id=execution_result.result_digest,
                )
                certificate = _build_certificate(
                    case_plan.validation.canonical_digest,
                    case_plan.logical_plan_id,
                    case_physical.physical_plan_id,
                    case_plan.bindings.policy_snapshot,
                    case_plan.bindings.data_snapshots,
                    case_physical.physical_operators,
                    execution_result.result_digest,
                    lineage_capture.evidence,
                    lineage_capture.lineage_digest,
                )
                certificate_check = verify_execution_certificate(
                    case_plan,
                    case_physical,
                    certificate,
                    observed_result_digest=execution_result.result_digest,
                )
                return latency_ms, execution_result, certificate_check, lineage_capture
            finally:
                connection.close()

        cold_latency_ms, cold_result, cold_certificate_check, cold_lineage_capture = execute_once()
        for _ in range(config.warmup_runs):
            execute_once()
        latencies: list[float] = []
        last_result = cold_result
        last_certificate_check = cold_certificate_check
        last_lineage_capture = cold_lineage_capture
        for _ in range(config.measured_runs):
            latency_ms, last_result, last_certificate_check, last_lineage_capture = execute_once()
            latencies.append(latency_ms)

        result_correct = _rows_match(last_result.rows, case.expected_rows)
        lineage_correct = (
            ObligationType.LINEAGE_CAPTURE in last_certificate_check.verified_obligations
            if case.source_lineage_required
            else last_lineage_capture.evidence is None
        )
        status_correct = (
            last_result.row_count == case.expected_row_count
            and result_correct
            and lineage_correct
            and last_certificate_check.diagnostics == ()
            and last_certificate_check.unverified_components == ("physical_plan_execution",)
        )
        rows.append(
            Phase1ExecutionResult(
                run_id=run_id,
                commit_hash=commit_hash,
                case_id=case.case_id,
                case_category=case.case_category,
                scenario=case.scenario,
                plan_id=str(case.raw_plan["plan_id"]),
                status="PASS" if status_correct else "FAIL",
                status_correct=status_correct,
                result_correct=result_correct,
                row_count=last_result.row_count,
                expected_row_count=case.expected_row_count,
                certificate_status=str(last_certificate_check.status),
                result_digest=last_result.result_digest,
                unverified_components=last_certificate_check.unverified_components,
                lineage_level=(
                    last_lineage_capture.evidence.lineage_level.value
                    if last_lineage_capture.evidence is not None
                    else LineageLevel.NONE.value
                ),
                lineage_source_count=last_lineage_capture.source_count,
                lineage_latency_ms=last_lineage_capture.latency_ms,
                verified_obligation_count=len(last_certificate_check.verified_obligations),
                cold_latency_ms=cold_latency_ms,
                median_latency_ms=statistics.median(latencies) if latencies else 0.0,
                p95_latency_ms=_percentile_95(latencies),
                min_latency_ms=min(latencies) if latencies else 0.0,
                max_latency_ms=max(latencies) if latencies else 0.0,
                sql_length=len(compiled.sql),
                parameter_count=len(compiled.parameters),
                logical_plan_id=validated_plan.logical_plan_id,
                physical_plan_id=physical.physical_plan_id,
            )
        )

    result_rows = tuple(rows)
    _write_csv(output_dir / "cases.csv", result_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "all_correct": all(result.status_correct for result in result_rows),
                "case_count": len(result_rows),
                "case_ids": [result.case_id for result in result_rows],
                "median_latency_ms": statistics.median(
                    result.median_latency_ms for result in result_rows
                ),
                "max_p95_latency_ms": max(result.p95_latency_ms for result in result_rows),
                "total_row_count": sum(result.row_count for result in result_rows),
                "result_correct": sum(result.result_correct for result in result_rows),
                "source_lineage_cases": sum(
                    result.lineage_level == LineageLevel.SOURCE.value for result in result_rows
                ),
                "max_lineage_latency_ms": max(result.lineage_latency_ms for result in result_rows),
                "certificate_statuses": sorted(
                    {result.certificate_status for result in result_rows}
                ),
                "unverified_components": sorted(
                    {item for result in result_rows for item in result.unverified_components}
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "environment.json").write_text(
        json.dumps(_environment(root, commit_hash), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "config.json").write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for result in result_rows:
        if not result.status_correct:
            (output_dir / "failures" / f"{result.case_id}.json").write_text(
                json.dumps(asdict(result), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    return output_dir
