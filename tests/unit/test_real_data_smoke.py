"""Small automated checks for real-data sampling and query equivalence."""

from __future__ import annotations

from pathlib import Path

import pytest

from trustaero.data.prepare import _even_source_order_sample
from trustaero.data.smoke import run_real_data_query_smoke

duckdb = pytest.importorskip("duckdb")


def _write_parquet(connection: object, path: Path, query: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(path).replace("'", "''")
    connection.execute(f"COPY ({query}) TO '{escaped}' (FORMAT PARQUET)")  # type: ignore[attr-defined]


def test_even_source_order_sample_is_exact_and_deterministic() -> None:
    connection = duckdb.connect()
    try:
        query = _even_source_order_sample(
            "range(10)",
            total_rows=10,
            sample_rows=4,
        )
        first = connection.execute(query).fetchall()
        second = connection.execute(query).fetchall()
    finally:
        connection.close()

    assert first == second == [(0,), (2,), (5,), (7,)]


def test_real_data_smoke_accepts_equal_nonempty_routes(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    bts_dir = data_root / "processed/bts/on_time/2024-01"
    nyc_dir = data_root / "processed/nyc_tlc/yellow/2024-01"
    connection = duckdb.connect()
    try:
        _write_parquet(
            connection,
            bts_dir / "bts_airports.parquet",
            "SELECT 1::INTEGER AS airport_id, 'AAA'::VARCHAR AS airport_code, "
            "'City'::VARCHAR AS city_name, 'CA'::VARCHAR AS state_code",
        )
        _write_parquet(
            connection,
            nyc_dir / "taxi_zones.parquet",
            "SELECT 1::BIGINT AS LocationID, 'Manhattan'::VARCHAR AS Borough, "
            "'Zone'::VARCHAR AS Zone, 'Yellow Zone'::VARCHAR AS service_zone",
        )
        for size in (100_000, 500_000):
            _write_parquet(
                connection,
                bts_dir / f"bts_flights_{size}.parquet",
                "SELECT * FROM (VALUES "
                "(1, 5.0, 1000.0, 0.0, DATE '2024-01-10'), "
                "(1, 7.0, 1200.0, 0.0, DATE '2024-01-11')) "
                "AS t(DestAirportID, ArrDelayMinutes, Distance, Cancelled, FlightDate)",
            )
            _write_parquet(
                connection,
                nyc_dir / f"yellow_taxi_{size}.parquet",
                "SELECT * FROM (VALUES "
                "(1, 3.0, 20.00, TIMESTAMP '2024-01-10 12:00:00'), "
                "(1, 4.0, 30.00, TIMESTAMP '2024-01-11 12:00:00')) "
                "AS t(PULocationID, trip_distance, total_amount, tpep_pickup_datetime)",
            )
    finally:
        connection.close()

    payload = run_real_data_query_smoke(data_root)

    assert payload["status"] == "PASS"
    assert len(payload["cases"]) == 4
    assert all(case["results_equal"] for case in payload["cases"])
    assert all(case["plans_distinct"] for case in payload["cases"])
    assert all(case["joined_rows"] == 2 for case in payload["cases"])
