"""End-to-end governed semantic smoke for official TPC-H SF1 Q6.

Q6 is the first official TPC-H query whose exact relational semantics fit the
reviewed IR fragment.  This module proves validation, candidate generation,
DuckDB execution, result equivalence, source lineage and certificate checking.
It intentionally records no latency and cannot be used as performance evidence.
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
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
)
from trustaero.experiments.tpch_audit import tpch_git_state, verify_tpch_artifact
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

TPCH_Q6_MATERIALIZATION_TARGETS = ("q06-time", "q06-predicate")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GovernedRealDataSmokeError(f"Expected a JSON object: {path}")
    return value


def _extension_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def create_tpch_q6_binding(connection: Any) -> TableBindings:
    """Bind the reviewed Q6 columns without altering the official base table."""

    # DATE is lifted to the IR's instant-preserving DATETIME contract. The
    # fixed-point numeric columns deliberately retain their physical DuckDB
    # DECIMAL representation, so official Q6 and every candidate compare
    # exactly instead of relying on a floating-point tolerance.
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_tpch_q6_lineitem AS SELECT "
        "CAST(l_shipdate AS TIMESTAMPTZ) AS l_shipdate, "
        "l_discount, l_quantity, l_extendedprice "
        "FROM lineitem"
    )
    return TableBindings(dataset_tables={"tpch_sf1_lineitem": "trust_tpch_q6_lineitem"})


def run_tpch_q6_semantic_smoke(project_root: Path, *, scale_factor: int = 1) -> dict[str, Any]:
    """Execute official Q6 and three equivalent approved physical routes."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for TPC-H Q6") from exc

    root = project_root.resolve()
    source_commit, source_dirty = tpch_git_state(root)
    if source_dirty:
        raise GovernedRealDataSmokeError(
            "TPC-H Q6 scientific smoke requires a clean committed source tree"
        )
    database, artifact = verify_tpch_artifact(root, scale_factor=scale_factor)
    examples = root / "examples/tpch"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_object(examples / "catalog.json"))
    )
    policy = PolicySet.model_validate(_load_object(examples / "policy.json"))
    response = validate(_load_object(examples / "plans/q06.json"), policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError(
            f"TPC-H Q6 expected REWRITE, received {response.status.value}"
        )
    logical: ValidatedLogicalPlan = response.validated_plan
    candidates = generate_duckdb_candidates(
        logical,
        materialization_targets=TPCH_Q6_MATERIALIZATION_TARGETS,
    )
    if len(candidates) != 3:
        raise GovernedRealDataSmokeError("TPC-H Q6 must generate exactly three candidates")

    extension_directory = (root / "data/processed/duckdb_extensions").resolve()
    connection = duckdb.connect(str(database), read_only=True)
    candidate_results: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET threads = 4")
        connection.execute(f"SET extension_directory = {_extension_literal(extension_directory)}")
        connection.execute("LOAD tpch")
        official_sql_row = connection.execute(
            "SELECT query FROM tpch_queries() WHERE query_nr = 6"
        ).fetchone()
        if official_sql_row is None:
            raise GovernedRealDataSmokeError("DuckDB TPC-H extension returned no Q6 SQL")
        official_rows = connection.execute(str(official_sql_row[0])).fetchall()
        if len(official_rows) != 1 or len(official_rows[0]) != 1:
            raise GovernedRealDataSmokeError("Official Q6 did not return one scalar")
        official_revenue = official_rows[0][0]

        bindings = create_tpch_q6_binding(connection)
        for candidate in candidates:
            compiled = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(compiled, connection)
            if execution.row_count != 1 or len(execution.rows[0]) != 1:
                raise GovernedRealDataSmokeError("Governed Q6 did not return one scalar")
            governed_revenue = execution.rows[0][0]
            if governed_revenue != official_revenue:
                raise GovernedRealDataSmokeError(
                    "Governed Q6 differs from the official result: "
                    f"{governed_revenue} vs {official_revenue}"
                )
            observation = observe_duckdb_plan(
                connection, compiled.sql, compiled.parameters, analyze=False
            )
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError(
                    "TPC-H Q6 candidates collapsed to duplicate physical plans"
                )
            fingerprints.add(observation.fingerprint)
            strategy_id = candidate.strategy.strategy_id
            candidate_results.append(
                {
                    "strategy_id": strategy_id,
                    "materialize_after": list(candidate.strategy.materialize_after),
                    "physical_plan_id": candidate.physical_plan_id,
                    "duckdb_plan_fingerprint": observation.fingerprint,
                    "duckdb_operator_names": list(observation.operator_names),
                    "row_count": execution.row_count,
                    "result_digest": execution.result_digest,
                    "official_result_equivalent": True,
                    "certificate_status": verify_candidate_execution_certificate(
                        logical,
                        candidate,
                        execution,
                        execution_id=f"tpch-q06-{strategy_id}",
                    ),
                }
            )
    finally:
        connection.close()

    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "query_id": "Q06",
        "semantic_contract": "exact_decimal_v2",
        "scale_factor": scale_factor,
        "purpose": (
            f"Official TPC-H SF{scale_factor} Q6 governed semantic smoke; no performance timing"
        ),
        "paper_performance_evidence": False,
        "raw_sql_bypass_used": False,
        "source_commit": source_commit,
        "source_dirty": False,
        "validation_status": response.status.value,
        "artifact_sha256": artifact["sha256"],
        "official_revenue_decimal": str(official_revenue),
        "candidate_count": len(candidates),
        "distinct_duckdb_plan_count": len(fingerprints),
        "all_official_result_equivalent": True,
        "candidates": candidate_results,
    }
    # Preserve the existing SF1 path because formal configs bind its SHA-256.
    output = (
        root / "results/tpch_q6_decimal_semantic_smoke/result.json"
        if scale_factor == 1
        else root / f"results/tpch_sf{scale_factor}_q6_semantic_smoke/result.json"
    )
    _atomic_json(output, payload)
    return payload
