"""Exact semantic smoke for the bounded TPC-H Query Coverage V2 adapters.

This module adds no performance claim.  It admits Q3 and Q10 only after every
governed physical candidate matches DuckDB's official ordered result, retains
a distinct physical plan, and produces a checked source-lineage certificate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.execution import (
    TableBindings,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_candidates import verify_candidate_execution_certificate
from trustaero.experiments.real_data_governed import GovernedRealDataSmokeError, _atomic_json
from trustaero.experiments.tpch_audit import tpch_git_state, verify_tpch_artifact
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

_ADAPTERS = {
    3: {
        "plan": "q03.json",
        "materialization_targets": ("q03-aggregate",),
        "contract": "three_table_revenue_top10",
    },
    10: {
        "plan": "q10.json",
        "materialization_targets": ("q10-aggregate",),
        "contract": "four_table_returned_order_top20",
    },
}


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GovernedRealDataSmokeError(f"Expected a JSON object: {path}")
    return value


def _extension_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _create_bindings(connection: Any) -> TableBindings:
    """Expose only reviewed fields from the frozen TPC-H relations.

    The DuckDB connection is fixed to UTC before these views are used. Keeping
    native DATE values also preserves the official Q3/Q10 output types and
    avoids a backend-only timezone-object dependency during result fetching.
    """

    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_tpch_v2_customer AS SELECT "
        "c_custkey, c_name, c_address, c_nationkey, c_phone, c_acctbal, "
        "c_mktsegment, c_comment FROM customer"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_tpch_v2_orders AS SELECT "
        "o_orderkey, o_custkey, o_orderdate, "
        "o_shippriority FROM orders"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_tpch_v2_lineitem AS SELECT "
        "l_orderkey, l_extendedprice, l_discount, l_shipdate, "
        "l_returnflag FROM lineitem"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_tpch_v2_nation AS SELECT n_nationkey, n_name FROM nation"
    )
    return TableBindings(
        dataset_tables={
            "tpch_sf1_customer": "trust_tpch_v2_customer",
            "tpch_sf1_orders": "trust_tpch_v2_orders",
            "tpch_sf1_lineitem_v2": "trust_tpch_v2_lineitem",
            "tpch_sf1_nation": "trust_tpch_v2_nation",
        }
    )


def run_tpch_query_coverage_v2_smoke(project_root: Path) -> dict[str, Any]:
    """Prove exact Q3/Q10 semantics on the frozen SF1 artifact."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for TPC-H V2 smoke") from exc

    root = project_root.resolve()
    source_commit, dirty = tpch_git_state(root)
    if dirty:
        raise GovernedRealDataSmokeError(
            "TPC-H Query Coverage V2 scientific smoke requires a clean commit"
        )
    database, artifact = verify_tpch_artifact(root, scale_factor=1)
    examples = root / "examples/tpch"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_object(examples / "catalog_v2.json")))
    policy = PolicySet.model_validate(_object(examples / "policy_v2.json"))

    connection = duckdb.connect(str(database), read_only=True)
    records: list[dict[str, Any]] = []
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET threads = 4")
        extension = (root / "data/processed/duckdb_extensions").resolve()
        connection.execute(f"SET extension_directory = {_extension_literal(extension)}")
        connection.execute("LOAD tpch")
        bindings = _create_bindings(connection)

        for query_number, adapter in _ADAPTERS.items():
            query_id = f"Q{query_number:02d}"
            response = validate(
                _object(examples / "plans" / str(adapter["plan"])),
                policy,
                catalog,
            )
            if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
                raise GovernedRealDataSmokeError(
                    f"{query_id} expected REWRITE, received {response.status.value}"
                )
            logical = response.validated_plan
            candidates = generate_duckdb_candidates(
                logical,
                materialization_targets=tuple(adapter["materialization_targets"]),
            )
            if len(candidates) != 2:
                raise GovernedRealDataSmokeError(f"{query_id} expected two candidates")

            official_row = connection.execute(
                "SELECT query FROM tpch_queries() WHERE query_nr = ?",
                [query_number],
            ).fetchone()
            if official_row is None:
                raise GovernedRealDataSmokeError(f"Official SQL missing for {query_id}")
            cursor = connection.execute(str(official_row[0]))
            official_rows = tuple(tuple(row) for row in cursor.fetchall())
            official_columns = tuple(str(item[0]) for item in cursor.description)

            fingerprints: set[str] = set()
            candidate_records: list[dict[str, Any]] = []
            for candidate in candidates:
                compiled = compile_approved_physical_plan(logical, candidate, catalog, bindings)
                execution = execute_with_connection(compiled, connection)
                if execution.columns != official_columns:
                    raise GovernedRealDataSmokeError(
                        f"{query_id} governed columns differ: "
                        f"{execution.columns!r} != {official_columns!r}"
                    )
                if execution.rows != official_rows:
                    raise GovernedRealDataSmokeError(
                        f"{query_id} governed rows differ from official TPC-H; "
                        f"governed_head={execution.rows[:2]!r}; "
                        f"official_head={official_rows[:2]!r}"
                    )
                observation = observe_duckdb_plan(
                    connection, compiled.sql, compiled.parameters, analyze=False
                )
                if observation.fingerprint in fingerprints:
                    raise GovernedRealDataSmokeError(
                        f"{query_id} candidates collapsed to one physical plan"
                    )
                fingerprints.add(observation.fingerprint)
                candidate_records.append(
                    {
                        "strategy_id": candidate.strategy.strategy_id,
                        "materialize_after": list(candidate.strategy.materialize_after),
                        "physical_plan_id": candidate.physical_plan_id,
                        "duckdb_plan_fingerprint": observation.fingerprint,
                        "duckdb_operator_names": list(observation.operator_names),
                        "result_digest": execution.result_digest,
                        "row_count": execution.row_count,
                        "official_result_equivalent": True,
                        "certificate_status": verify_candidate_execution_certificate(
                            logical,
                            candidate,
                            execution,
                            execution_id=f"tpch-v2-{query_id.lower()}-"
                            f"{candidate.strategy.strategy_id}",
                        ),
                    }
                )
            records.append(
                {
                    "query_id": query_id,
                    "semantic_contract": adapter["contract"],
                    "validation_status": response.status.value,
                    "official_columns": list(official_columns),
                    "official_row_count": len(official_rows),
                    "candidate_count": len(candidates),
                    "distinct_duckdb_plan_count": len(fingerprints),
                    "candidates": candidate_records,
                }
            )
    finally:
        connection.close()

    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS_TPCH_QUERY_COVERAGE_V2_SEMANTICS",
        "source_commit": source_commit,
        "source_dirty": False,
        "artifact_sha256": artifact["sha256"],
        "official_query_denominator": 22,
        "previous_exact_support": ["Q01", "Q06"],
        "new_exact_support": ["Q03", "Q10"],
        "exact_support_after_smoke": ["Q01", "Q03", "Q06", "Q10"],
        "exact_support_count_after_smoke": 4,
        "paper_performance_evidence": False,
        "raw_sql_bypass_used": False,
        "scientific_boundary": (
            "This smoke authorizes exact semantic support for Q3 and Q10. "
            "It records no latency and makes no optimizer-generalization claim."
        ),
        "queries": records,
    }
    _atomic_json(root / "results/tpch_query_coverage_v2/result.json", payload)
    return payload
