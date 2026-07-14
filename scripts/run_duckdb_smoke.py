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
from trustaero.ir.models import PolicySet
from trustaero.validator.service import validate

ROOT = Path(__file__).resolve().parents[1]


def _load_json(relative: str) -> dict[str, Any]:
    loaded = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object at {relative}")
    return loaded


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

    print(
        json.dumps(
            {
                "logical_plan_id": compiled.logical_plan_id,
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
