"""End-to-end governed semantic smoke for official TPC-H SF1 Q1.

Q1 exercises grouping, exact fixed-point formulas and deterministic sorting.
The smoke compares every approved candidate with DuckDB's official query and
then verifies source-lineage execution certificates. It intentionally records
no latency, so its output is semantic evidence rather than performance data.
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
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

TPCH_Q1_MATERIALIZATION_TARGETS = ("q01-filter", "q01-aggregate")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GovernedRealDataSmokeError(f"Expected a JSON object: {path}")
    return value


def _extension_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def create_tpch_q1_binding(connection: Any) -> TableBindings:
    """Expose only the reviewed Q1 columns through a trusted temporary view."""

    # The UTC cast makes DATE comparison independent from the host session.
    # DuckDB DECIMAL columns remain fixed point throughout all Q1 formulas.
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_tpch_q1_lineitem AS SELECT "
        "l_returnflag, l_linestatus, CAST(l_shipdate AS TIMESTAMPTZ) AS l_shipdate, "
        "l_discount, l_tax, l_quantity, l_extendedprice FROM lineitem"
    )
    return TableBindings(dataset_tables={"tpch_sf1_lineitem": "trust_tpch_q1_lineitem"})


def run_tpch_q1_semantic_smoke(project_root: Path, *, scale_factor: int = 1) -> dict[str, Any]:
    """Execute official Q1 and three equivalent governed physical routes."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for TPC-H Q1") from exc

    root = project_root.resolve()
    source_commit, source_dirty = tpch_git_state(root)
    if source_dirty:
        raise GovernedRealDataSmokeError(
            "TPC-H Q1 scientific smoke requires a clean committed source tree"
        )
    database, artifact = verify_tpch_artifact(root, scale_factor=scale_factor)
    examples = root / "examples/tpch"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_object(examples / "catalog.json"))
    )
    policy = PolicySet.model_validate(_load_object(examples / "policy.json"))
    response = validate(_load_object(examples / "plans/q01.json"), policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError(
            f"TPC-H Q1 expected REWRITE, received {response.status.value}"
        )
    logical: ValidatedLogicalPlan = response.validated_plan
    candidates = generate_duckdb_candidates(
        logical,
        materialization_targets=TPCH_Q1_MATERIALIZATION_TARGETS,
    )
    if len(candidates) != 3:
        raise GovernedRealDataSmokeError("TPC-H Q1 must generate exactly three candidates")

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
            "SELECT query FROM tpch_queries() WHERE query_nr = 1"
        ).fetchone()
        if official_sql_row is None:
            raise GovernedRealDataSmokeError("DuckDB TPC-H extension returned no Q1 SQL")
        official_cursor = connection.execute(str(official_sql_row[0]))
        official_rows = official_cursor.fetchall()
        official_columns = tuple(str(item[0]) for item in official_cursor.description)

        bindings = create_tpch_q1_binding(connection)
        for candidate in candidates:
            compiled = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(compiled, connection)
            if execution.columns != official_columns:
                raise GovernedRealDataSmokeError(
                    "Governed Q1 columns differ from the official result schema"
                )
            if execution.rows != tuple(official_rows):
                raise GovernedRealDataSmokeError(
                    "Governed Q1 rows differ from the ordered official result"
                )
            observation = observe_duckdb_plan(
                connection, compiled.sql, compiled.parameters, analyze=False
            )
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError(
                    "TPC-H Q1 candidates collapsed to duplicate physical plans"
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
                        execution_id=f"tpch-q01-{strategy_id}",
                    ),
                }
            )
    finally:
        connection.close()

    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "query_id": "Q01",
        "semantic_contract": "bounded_decimal_formula_and_sort_v1",
        "scale_factor": scale_factor,
        "purpose": (
            f"Official TPC-H SF{scale_factor} Q1 governed semantic smoke; no performance timing"
        ),
        "paper_performance_evidence": False,
        "raw_sql_bypass_used": False,
        "source_commit": source_commit,
        "source_dirty": False,
        "validation_status": response.status.value,
        "artifact_sha256": artifact["sha256"],
        "official_row_count": len(official_rows),
        "official_columns": list(official_columns),
        "candidate_count": len(candidates),
        "distinct_duckdb_plan_count": len(fingerprints),
        "all_official_result_equivalent": True,
        "candidates": candidate_results,
    }
    # Preserve the content-addressed SF1 location already bound by the frozen
    # formal protocol; new scales use an explicit scale-qualified directory.
    output = (
        root / "results/tpch_q1_decimal_semantic_smoke/result.json"
        if scale_factor == 1
        else root / f"results/tpch_sf{scale_factor}_q1_semantic_smoke/result.json"
    )
    _atomic_json(output, payload)
    return payload
