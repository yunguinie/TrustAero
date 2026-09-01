"""Correctness smoke tests over prepared BTS and NYC real-data slices.

The smoke compares two deliberately different but semantically equivalent
DuckDB routes: a fused query and a route that materializes the governed filter
before the Join. It verifies equal ordered results and distinct physical-plan
fingerprints. No latency from this module is a paper result.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RealDataSmokeError(RuntimeError):
    """Raised when real-data candidate plans disagree or collapse."""


@dataclass(frozen=True, slots=True)
class RealDataSmokeResult:
    """Correctness and plan-shape evidence for one dataset/scale case."""

    case_id: str
    dataset_id: str
    input_rows: int
    filtered_rows: int
    joined_rows: int
    result_rows: int
    result_digest: str
    fused_plan_digest: str
    materialized_plan_digest: str
    plans_distinct: bool
    results_equal: bool


def _sql_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result_digest(rows: list[tuple[Any, ...]]) -> str:
    """Hash ordered result values using a stable JSON representation."""

    payload = json.dumps(rows, default=str, ensure_ascii=False, separators=(",", ":"))
    return _digest_text(payload)


def _explain(connection: Any, query: str) -> str:
    rows = connection.execute(f"EXPLAIN {query}").fetchall()
    return "\n".join(str(row[-1]) for row in rows)


def _scalar_count(connection: Any, query: str) -> int:
    row = connection.execute(query).fetchone()
    if row is None:
        raise RealDataSmokeError("DuckDB returned no row for a count query")
    return int(row[0])


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def _run_equivalent_routes(
    connection: Any,
    *,
    case_id: str,
    dataset_id: str,
    input_source: str,
    dimension_source: str,
    filtered_projection: str,
    filter_predicate: str,
    join_predicate: str,
    final_projection: str,
    group_order_clause: str,
    temp_table: str,
) -> RealDataSmokeResult:
    """Run and compare fused versus governed-filter materialization routes."""

    fused_query = (
        f"SELECT {final_projection} FROM "
        f"(SELECT {filtered_projection} FROM {input_source} WHERE {filter_predicate}) f "
        f"JOIN {dimension_source} d ON {join_predicate} "
        f"{group_order_clause}"
    )
    materialized_select = (
        f"SELECT {final_projection} FROM {temp_table} f "
        f"JOIN {dimension_source} d ON {join_predicate} "
        f"{group_order_clause}"
    )
    materialize = (
        f"CREATE OR REPLACE TEMP TABLE {temp_table} AS "
        f"SELECT {filtered_projection} FROM {input_source} WHERE {filter_predicate}"
    )

    fused_plan = _explain(connection, fused_query)
    fused_rows = connection.execute(fused_query).fetchall()
    materialization_plan = _explain(connection, materialize)
    connection.execute(materialize)
    materialized_plan = materialization_plan + "\n" + _explain(connection, materialized_select)
    materialized_rows = connection.execute(materialized_select).fetchall()

    results_equal = fused_rows == materialized_rows
    if not results_equal:
        raise RealDataSmokeError(f"Candidate result mismatch for {case_id}")
    fused_plan_digest = _digest_text(fused_plan)
    materialized_plan_digest = _digest_text(materialized_plan)
    plans_distinct = fused_plan_digest != materialized_plan_digest
    if not plans_distinct:
        raise RealDataSmokeError(f"DuckDB physical plans unexpectedly collapsed for {case_id}")

    input_rows = _scalar_count(connection, f"SELECT count(*) FROM {input_source}")
    filtered_rows = _scalar_count(
        connection, f"SELECT count(*) FROM {input_source} WHERE {filter_predicate}"
    )
    joined_rows = _scalar_count(
        connection,
        f"SELECT count(*) FROM {temp_table} f JOIN {dimension_source} d ON {join_predicate}",
    )
    connection.execute(f"DROP TABLE {temp_table}")
    if filtered_rows == 0 or joined_rows == 0 or not fused_rows:
        raise RealDataSmokeError(
            f"Smoke query produced no meaningful rows for {case_id}: "
            f"filtered={filtered_rows}, joined={joined_rows}, result_groups={len(fused_rows)}"
        )
    return RealDataSmokeResult(
        case_id=case_id,
        dataset_id=dataset_id,
        input_rows=input_rows,
        filtered_rows=filtered_rows,
        joined_rows=joined_rows,
        result_rows=len(fused_rows),
        result_digest=_result_digest(fused_rows),
        fused_plan_digest=fused_plan_digest,
        materialized_plan_digest=materialized_plan_digest,
        plans_distinct=plans_distinct,
        results_equal=results_equal,
    )


def run_real_data_query_smoke(data_root: Path) -> dict[str, Any]:
    """Run fixed BTS/NYC smoke queries at 100K and 500K rows."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RealDataSmokeError("DuckDB is required for real-data smoke queries") from exc

    root = data_root.resolve()
    bts_dir = root / "processed/bts/on_time/2024-01"
    nyc_dir = root / "processed/nyc_tlc/yellow/2024-01"
    airport_path = bts_dir / "bts_airports.parquet"
    zone_path = nyc_dir / "taxi_zones.parquet"
    required = [
        airport_path,
        zone_path,
        *(bts_dir / f"bts_flights_{size}.parquet" for size in (100_000, 500_000)),
        *(nyc_dir / f"yellow_taxi_{size}.parquet" for size in (100_000, 500_000)),
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RealDataSmokeError(f"Prepared artifacts are missing: {', '.join(missing)}")

    connection = duckdb.connect()
    results: list[RealDataSmokeResult] = []
    try:
        connection.execute("SET memory_limit = '4GB'")
        connection.execute(f"SET temp_directory = {_sql_literal(root / 'tmp/duckdb')}")
        connection.execute("SET preserve_insertion_order = true")

        for size in (100_000, 500_000):
            bts_path = bts_dir / f"bts_flights_{size}.parquet"
            results.append(
                _run_equivalent_routes(
                    connection,
                    case_id=f"bts_join_{size}",
                    dataset_id="bts_on_time_2024_01",
                    input_source=f"read_parquet({_sql_literal(bts_path)})",
                    dimension_source=f"read_parquet({_sql_literal(airport_path)})",
                    filtered_projection=(
                        "DestAirportID, ArrDelayMinutes, Distance, Cancelled, FlightDate"
                    ),
                    filter_predicate=(
                        "FlightDate >= DATE '2024-01-08' AND FlightDate < DATE '2024-01-22' "
                        "AND Cancelled = 0 AND Distance >= 750"
                    ),
                    join_predicate="f.DestAirportID = d.airport_id",
                    final_projection=(
                        "d.state_code, count(*) AS flight_count, "
                        "sum(CAST(round(f.ArrDelayMinutes) AS BIGINT)) "
                        "AS total_arrival_delay_minutes"
                    ),
                    group_order_clause="GROUP BY d.state_code ORDER BY d.state_code",
                    temp_table=f"bts_filtered_{size}",
                )
            )

            nyc_path = nyc_dir / f"yellow_taxi_{size}.parquet"
            results.append(
                _run_equivalent_routes(
                    connection,
                    case_id=f"nyc_join_{size}",
                    dataset_id="nyc_tlc_yellow_2024_01",
                    input_source=f"read_parquet({_sql_literal(nyc_path)})",
                    dimension_source=f"read_parquet({_sql_literal(zone_path)})",
                    filtered_projection=(
                        "PULocationID, trip_distance, total_amount, tpep_pickup_datetime"
                    ),
                    filter_predicate=(
                        "tpep_pickup_datetime >= TIMESTAMP '2024-01-08 00:00:00' "
                        "AND tpep_pickup_datetime < TIMESTAMP '2024-01-22 00:00:00' "
                        "AND trip_distance >= 2 AND total_amount >= 10"
                    ),
                    join_predicate="f.PULocationID = d.LocationID",
                    final_projection=(
                        "d.Borough, d.service_zone, count(*) AS trip_count, "
                        "sum(CAST(round(f.total_amount * 100) AS BIGINT)) "
                        "AS total_amount_cents"
                    ),
                    group_order_clause=(
                        "GROUP BY d.Borough, d.service_zone ORDER BY d.Borough, d.service_zone"
                    ),
                    temp_table=f"nyc_filtered_{size}",
                )
            )
    finally:
        connection.close()

    payload = {
        "schema_version": 1,
        "run_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "correctness and physical-plan smoke; timings are intentionally absent",
        "status": "PASS",
        "cases": [asdict(result) for result in results],
    }
    _atomic_json(root / "manifests/processed/real-data-query-smoke.json", payload)
    return payload
