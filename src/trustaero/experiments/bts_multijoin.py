"""Semantic-only BTS fact/airport/carrier natural multi-Join smoke.

The smoke proves that the reviewed IR query has multiple result-equivalent
DuckDB routes and that each route can be tied to source lineage and a checked
certificate.  It intentionally records no latency and cannot support a
performance claim.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_bts_multijoin_slice_artifacts
from trustaero.execution import (
    TableBindings,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_candidates import (
    verify_candidate_execution_certificate,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _semantic_digest
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

BTS_MULTIJOIN_TARGETS = (
    "bts-mj-filter",
    "bts-mj-origin-join",
    "bts-mj-carrier-join",
)


def _create_bts_multijoin_views(
    connection: Any,
    data_root: Path,
    *,
    sample_rows: int,
    full_month: bool = False,
) -> TableBindings:
    """Expose only reviewed fields with stable logical types."""

    base = data_root / "processed/bts/on_time/2024-01"
    flights = (
        base / "bts_flights_full.parquet"
        if full_month
        else base / f"bts_flights_{sample_rows}.parquet"
    )
    airports = base / "bts_airports.parquet"
    carriers = base / "bts_carriers.parquet"
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mj_flights AS SELECT "
        "CAST(FlightDate AS TIMESTAMPTZ) AS FlightDate, "
        "CAST(OriginAirportID AS BIGINT) AS OriginAirportID, "
        "CAST(DOT_ID_Reporting_Airline AS BIGINT) AS DOT_ID_Reporting_Airline, "
        "CAST(Distance AS DOUBLE) AS Distance, "
        "CAST(Cancelled AS BOOLEAN) AS Cancelled "
        f"FROM read_parquet({_sql_literal(flights)})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mj_airports AS SELECT "
        "CAST(airport_id AS BIGINT) AS airport_id, "
        "CAST(airport_code AS VARCHAR) AS airport_code, "
        "CAST(city_name AS VARCHAR) AS city_name, "
        "CAST(state_code AS VARCHAR) AS state_code "
        f"FROM read_parquet({_sql_literal(airports)})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mj_carriers AS SELECT "
        "CAST(carrier_id AS BIGINT) AS carrier_id, "
        "CAST(carrier_code AS VARCHAR) AS carrier_code "
        f"FROM read_parquet({_sql_literal(carriers)})"
    )
    return TableBindings(
        dataset_tables={
            "bts_on_time_2024_01_multijoin": "trust_bts_mj_flights",
            "bts_airports_2024_01": "trust_bts_mj_airports",
            "bts_carriers_2024_01": "trust_bts_mj_carriers",
        }
    )


def run_bts_multijoin_smoke(
    project_root: Path,
    *,
    sample_rows: int = 100_000,
) -> dict[str, Any]:
    """Validate and execute fused plus three materialized natural-Join routes."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for BTS multi-Join") from exc

    root = project_root.resolve()
    artifacts = verify_bts_multijoin_slice_artifacts(root / "data", sample_rows)
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_multijoin_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_multijoin_policy.json"))
    raw_plan = _load_json(examples / "plans/bts_natural_multijoin.json")
    response = validate(raw_plan, policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("BTS natural multi-Join did not validate as expected")
    logical: ValidatedLogicalPlan = response.validated_plan
    candidates = generate_duckdb_candidates(
        logical,
        materialization_targets=BTS_MULTIJOIN_TARGETS,
    )

    connection = duckdb.connect()
    results: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    expected_digest: str | None = None
    try:
        connection.execute("SET threads = 4")
        bindings = _create_bts_multijoin_views(
            connection,
            root / "data",
            sample_rows=sample_rows,
        )
        for candidate in candidates:
            compiled = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(compiled, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError(
                    "BTS multi-Join candidates returned different relations"
                )
            observation = observe_duckdb_plan(
                connection,
                compiled.sql,
                compiled.parameters,
                analyze=False,
            )
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError(
                    "BTS multi-Join candidate collapsed to a duplicate DuckDB plan"
                )
            fingerprints.add(observation.fingerprint)
            strategy_id = candidate.strategy.strategy_id
            certificate_status = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"bts-multijoin-{strategy_id}",
            )
            results.append(
                {
                    "strategy_id": strategy_id,
                    "materialize_after": list(candidate.strategy.materialize_after),
                    "physical_plan_id": candidate.physical_plan_id,
                    "duckdb_plan_fingerprint": observation.fingerprint,
                    "duckdb_operator_names": list(observation.operator_names),
                    "output_row_count": execution.row_count,
                    "semantic_result_digest": digest,
                    "certificate_status": certificate_status,
                }
            )
    finally:
        connection.close()

    if len(candidates) != 4 or len(fingerprints) != 4:
        raise GovernedRealDataSmokeError("BTS multi-Join did not retain four physical routes")
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "purpose": "BTS natural multi-Join semantic smoke; no performance timing",
        "paper_performance_evidence": False,
        "sample_rows": sample_rows,
        "verified_execution_artifacts": [asdict(item) for item in artifacts],
        "candidate_count": len(candidates),
        "distinct_duckdb_plan_count": len(fingerprints),
        "candidates": results,
    }
    _atomic_json(
        root / "data/manifests/processed/bts-multijoin-semantic-smoke.json",
        payload,
    )
    return payload
