"""Run the minimal TrustAero-to-DuckDB smoke path.

This script is intentionally tiny: it validates one example plan, compiles the
validated plan into parameterized SQL, creates a small in-memory DuckDB table,
and prints a result digest that can later be bound into an execution
certificate. It is not a benchmark.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import TableBindings, compile_validated_plan, execute_with_connection
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import ExecutionEvent, GovernedExecutionCertificate, PolicySet
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.certificate import verify_execution_certificate
from trustaero.validator.service import validate

ROOT = Path(__file__).resolve().parents[1]


def _load_json(relative: str) -> dict[str, Any]:
    loaded = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object at {relative}")
    return loaded


def _event(
    sequence: int,
    event_type: str,
    payload_digest: str,
    operator_id: str | None = None,
) -> ExecutionEvent:
    """Build a minimal trusted-executor event for the smoke certificate."""

    return ExecutionEvent(
        sequence=sequence,
        event_type=event_type,
        operator_id=operator_id,
        payload_digest=payload_digest,
    )


def main() -> int:
    try:
        duckdb: Any = importlib.import_module("duckdb")
    except ModuleNotFoundError:
        print('DuckDB is optional. Install it with: python -m pip install -e ".[dev,duckdb]"')
        return 2

    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json("examples/catalogs/minimal_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json("examples/policies/research_policy.json"))
    response = validate(_load_json("examples/plans/accept_earthquakes.json"), policy, catalog)
    if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
        print(response.model_dump_json(indent=2))
        return 1
    if response.validated_plan is None:
        print("Validator accepted the plan but did not return a validated logical plan.")
        return 1

    compiled = compile_validated_plan(
        response.validated_plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
    )

    connection = duckdb.connect(":memory:")
    try:
        # The table is created by trusted experiment code, not by the agent plan.
        connection.execute(
            """
            CREATE TABLE earthquake_events(
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
        result = execute_with_connection(compiled, connection)
    finally:
        connection.close()

    physical = plan_physical_execution(response.validated_plan)
    events: list[ExecutionEvent] = [_event(0, "PlanApproved", physical.physical_plan_id)]
    sequence = 1
    for operator in physical.physical_operators:
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
    events.append(_event(sequence, "ResultMaterialized", result.result_digest))
    sequence += 1
    events.append(_event(sequence, "CertificateEmitted", "sha256:certificate"))
    certificate = GovernedExecutionCertificate(
        certificate_id="cert-duckdb-smoke",
        task_digest=response.validated_plan.validation.canonical_digest,
        logical_plan_id=response.validated_plan.logical_plan_id,
        physical_plan_id=physical.physical_plan_id,
        policy_snapshot=response.validated_plan.bindings.policy_snapshot,
        data_snapshots=response.validated_plan.bindings.data_snapshots,
        events=tuple(events),
        result_digest=result.result_digest,
    )
    certificate_check = verify_execution_certificate(
        response.validated_plan,
        physical,
        certificate,
        observed_result_digest=result.result_digest,
    )

    print(
        json.dumps(
            {
                "logical_plan_id": compiled.logical_plan_id,
                "certificate_status": certificate_check.status,
                "certificate_unverified_components": certificate_check.unverified_components,
                "row_count": result.row_count,
                "columns": result.columns,
                "result_digest": result.result_digest,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
