"""Phase 1 DuckDB-backed minimal execution experiment runner.

Phase 1 turns the earlier smoke script into a repeatable artifact. It measures a
single trusted path: validated logical plan -> parameterized SQL -> DuckDB
materialization -> result digest -> certificate verification.
"""

from __future__ import annotations

import csv
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import TableBindings, compile_validated_plan, execute_with_connection
from trustaero.experiments.loader import load_json
from trustaero.experiments.models import Phase1Config, Phase1ExecutionResult
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import ExecutionEvent, GovernedExecutionCertificate, PolicySet
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
            event_time TIMESTAMP,
            latitude DOUBLE,
            longitude DOUBLE,
            magnitude DOUBLE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO earthquake_events VALUES
          ('eq-001', TIMESTAMP '2026-06-01 00:00:00', 39.9, 116.4, 4.8),
          ('eq-002', TIMESTAMP '2026-06-02 00:00:00', 40.1, 116.2, 5.1)
        """
    )


def _build_certificate(
    plan_digest: str,
    logical_plan_id: str,
    physical_plan_id: str,
    policy_snapshot: str,
    data_snapshots: dict[str, str],
    physical_operators: tuple[Any, ...],
    result_digest: str,
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
    raw_plan = load_json(root / "examples/plans/accept_earthquakes.json")
    response = validate(raw_plan, policy, catalog)
    if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
        raise RuntimeError(f"Phase 1 baseline did not validate: {response.status}")
    if response.validated_plan is None:
        raise RuntimeError("Validator returned no ValidatedLogicalPlan for Phase 1 baseline.")
    validated_plan = response.validated_plan

    compiled = compile_validated_plan(
        validated_plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
    )
    physical = plan_physical_execution(validated_plan)

    def execute_once() -> tuple[float, Any, Any]:
        """Run one isolated in-memory DuckDB execution and measure wall time."""

        connection = duckdb.connect(":memory:")
        try:
            _prepare_duckdb_table(connection)
            started = time.perf_counter()
            execution_result = execute_with_connection(compiled, connection)
            latency_ms = (time.perf_counter() - started) * 1000.0
            certificate = _build_certificate(
                validated_plan.validation.canonical_digest,
                validated_plan.logical_plan_id,
                physical.physical_plan_id,
                validated_plan.bindings.policy_snapshot,
                validated_plan.bindings.data_snapshots,
                physical.physical_operators,
                execution_result.result_digest,
            )
            certificate_check = verify_execution_certificate(
                validated_plan,
                physical,
                certificate,
                observed_result_digest=execution_result.result_digest,
            )
            return latency_ms, execution_result, certificate_check
        finally:
            connection.close()

    cold_latency_ms, cold_result, cold_certificate_check = execute_once()
    for _ in range(config.warmup_runs):
        execute_once()
    latencies: list[float] = []
    last_result = cold_result
    last_certificate_check = cold_certificate_check
    for _ in range(config.measured_runs):
        latency_ms, last_result, last_certificate_check = execute_once()
        latencies.append(latency_ms)

    expected_row_count = 2
    status_correct = (
        last_result.row_count == expected_row_count
        and last_certificate_check.diagnostics == ()
        and last_certificate_check.unverified_components == ("physical_plan_execution",)
    )
    result = Phase1ExecutionResult(
        run_id=run_id,
        commit_hash=commit_hash,
        case_id="P1-001",
        plan_id=str(raw_plan["plan_id"]),
        status="PASS" if status_correct else "FAIL",
        status_correct=status_correct,
        row_count=last_result.row_count,
        expected_row_count=expected_row_count,
        certificate_status=str(last_certificate_check.status),
        result_digest=last_result.result_digest,
        unverified_components=last_certificate_check.unverified_components,
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

    rows = (result,)
    _write_csv(output_dir / "cases.csv", rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "all_correct": result.status_correct,
                "case_count": len(rows),
                "median_latency_ms": result.median_latency_ms,
                "p95_latency_ms": result.p95_latency_ms,
                "row_count": result.row_count,
                "result_digest": result.result_digest,
                "certificate_status": result.certificate_status,
                "unverified_components": result.unverified_components,
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
    if not result.status_correct:
        (output_dir / "failures" / f"{result.case_id}.json").write_text(
            json.dumps(asdict(result), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return output_dir
