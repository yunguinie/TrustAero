"""Clean-room check for the governed real-data execution boundary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from trustaero.data.download import sha256_file
from trustaero.experiments.bts_mask_join import run_bts_mask_join_smoke
from trustaero.experiments.bts_multijoin import run_bts_multijoin_smoke
from trustaero.experiments.real_data_candidate_analysis import (
    analyze_real_data_candidate_pilot,
)
from trustaero.experiments.real_data_candidate_pilot import (
    RealDataCandidatePilotConfig,
    complete_permutation_orders,
    run_real_data_candidate_pilot,
)
from trustaero.experiments.real_data_candidates import run_real_data_candidate_smoke
from trustaero.experiments.real_data_governed import run_governed_real_data_smoke
from trustaero.experiments.real_data_pilot import (
    RealDataPilotConfig,
    run_real_data_pilot,
)
from trustaero.experiments.real_data_pilot_analysis import analyze_real_data_pilot

duckdb = pytest.importorskip("duckdb")


def test_complete_permutation_orders_balance_every_position() -> None:
    strategies = ("fused", "early", "late")

    orders = complete_permutation_orders(strategies, 12, seed=7)

    assert len(set(orders)) == 6
    assert all(orders.count(order) == 2 for order in set(orders))
    for strategy in strategies:
        assert [sum(order[position] == strategy for order in orders) for position in range(3)] == [
            4,
            4,
            4,
        ]


def _write_parquet(connection: object, path: Path, query: str) -> None:
    """Create a tiny fixture with DuckDB rather than committing binary data."""

    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(path).replace("'", "''")
    connection.execute(f"COPY ({query}) TO '{escaped}' (FORMAT PARQUET)")  # type: ignore[attr-defined]


def _write_slice_manifest(data_root: Path, sample_rows: int, zone_rows: int) -> None:
    """Bind tiny generated Parquet fixtures exactly as the preparation step does."""

    paths = (
        (
            f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet",
            2,
        ),
        (
            f"processed/nyc_tlc/yellow/2024-01/yellow_taxi_{sample_rows}.parquet",
            2 if sample_rows == 100_000 else 1,
        ),
        ("processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet", zone_rows),
    )
    outputs = []
    for index, (relative, row_count) in enumerate(paths):
        path = data_root / relative
        outputs.append(
            {
                "artifact_id": f"test-artifact-{index}",
                "relative_path": relative,
                "row_count": row_count,
                "byte_size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = data_root / "manifests/processed/real-data-smoke.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"outputs": outputs}), encoding="utf-8")


def _write_bts_multijoin_manifest(data_root: Path, sample_rows: int) -> None:
    """Bind the tiny fact and dimension fixtures for the natural-Join smoke."""

    definitions = (
        (
            f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet",
            3,
        ),
        ("processed/bts/on_time/2024-01/bts_airports.parquet", 2),
        ("processed/bts/on_time/2024-01/bts_carriers.parquet", 2),
    )
    outputs = []
    for index, (relative_path, row_count) in enumerate(definitions):
        path = data_root / relative_path
        outputs.append(
            {
                "artifact_id": f"bts-multijoin-test-{index}",
                "relative_path": relative_path,
                "row_count": row_count,
                "byte_size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = data_root / "manifests/processed/real-data-smoke.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"outputs": outputs}), encoding="utf-8")


def _write_bts_mask_join_manifest(data_root: Path, sample_rows: int) -> None:
    """Bind only the fact and airport files consumed by the Mask/Join smoke."""

    definitions = (
        (
            f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet",
            3,
        ),
        ("processed/bts/on_time/2024-01/bts_airports.parquet", 2),
    )
    outputs = []
    for index, (relative_path, row_count) in enumerate(definitions):
        path = data_root / relative_path
        outputs.append(
            {
                "artifact_id": f"bts-mask-join-test-{index}",
                "relative_path": relative_path,
                "row_count": row_count,
                "byte_size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = data_root / "manifests/processed/real-data-smoke.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"outputs": outputs}), encoding="utf-8")


def test_bts_mask_join_moves_only_non_key_sensitive_field(tmp_path: Path) -> None:
    """Early Mask must preserve results and satisfy the strict raw-Join policy."""

    repository_root = Path(__file__).resolve().parents[2]
    shutil.copytree(repository_root / "examples/real_data", tmp_path / "examples/real_data")
    base = tmp_path / "data/processed/bts/on_time/2024-01"
    connection = duckdb.connect()
    try:
        _write_parquet(
            connection,
            base / "bts_flights_3.parquet",
            "SELECT * FROM (VALUES "
            "(DATE '2024-01-10', 1, 'N001AA', 1000.0, false), "
            "(DATE '2024-01-11', 2, 'N002BB', 900.0, false), "
            "(DATE '2024-01-25', 1, 'N003CC', 1200.0, false)) "
            "AS t(FlightDate, OriginAirportID, Tail_Number, Distance, Cancelled)",
        )
        _write_parquet(
            connection,
            base / "bts_airports.parquet",
            "SELECT * FROM (VALUES (1, 'AAA', 'Alpha', 'AA'), "
            "(2, 'BBB', 'Beta', 'BB')) "
            "AS t(airport_id, airport_code, city_name, state_code)",
        )
    finally:
        connection.close()
    _write_bts_mask_join_manifest(tmp_path / "data", 3)

    payload = run_bts_mask_join_smoke(tmp_path, sample_rows=3)

    assert payload["status"] == "PASS"
    assert payload["paper_performance_evidence"] is False
    assert payload["candidate_count"] == 2
    assert payload["distinct_duckdb_plan_count"] == 2
    assert payload["filtered_rows_entering_join"] == 2
    assert payload["strict_policy_forces_early_mask"] is True
    assert {item["output_row_count"] for item in payload["candidates"]} == {2}
    assert len({item["semantic_result_digest"] for item in payload["candidates"]}) == 1
    strict = payload["governance_profiles"]["no-raw-sensitive-join"]
    assert strict["rejected_candidate_ids"] == ["fused"]


def test_bts_natural_multijoin_has_four_equal_physical_routes(tmp_path: Path) -> None:
    """Exercise the native fact-airport-carrier Join without timing it."""

    repository_root = Path(__file__).resolve().parents[2]
    shutil.copytree(repository_root / "examples/real_data", tmp_path / "examples/real_data")
    base = tmp_path / "data/processed/bts/on_time/2024-01"
    connection = duckdb.connect()
    try:
        _write_parquet(
            connection,
            base / "bts_flights_3.parquet",
            "SELECT * FROM (VALUES "
            "(DATE '2024-01-10', 1, 10, 1000.0, false), "
            "(DATE '2024-01-11', 2, 20, 900.0, false), "
            "(DATE '2024-01-25', 1, 10, 1200.0, false)) "
            "AS t(FlightDate, OriginAirportID, DOT_ID_Reporting_Airline, "
            "Distance, Cancelled)",
        )
        _write_parquet(
            connection,
            base / "bts_airports.parquet",
            "SELECT * FROM (VALUES (1, 'AAA', 'Alpha', 'AA'), "
            "(2, 'BBB', 'Beta', 'BB')) "
            "AS t(airport_id, airport_code, city_name, state_code)",
        )
        _write_parquet(
            connection,
            base / "bts_carriers.parquet",
            "SELECT * FROM (VALUES (10, 'C1'), (20, 'C2')) AS t(carrier_id, carrier_code)",
        )
    finally:
        connection.close()
    _write_bts_multijoin_manifest(tmp_path / "data", 3)

    payload = run_bts_multijoin_smoke(tmp_path, sample_rows=3)

    assert payload["status"] == "PASS"
    assert payload["paper_performance_evidence"] is False
    assert payload["candidate_count"] == 4
    assert payload["distinct_duckdb_plan_count"] == 4
    assert {item["output_row_count"] for item in payload["candidates"]} == {2}
    assert len({item["semantic_result_digest"] for item in payload["candidates"]}) == 1


def test_governed_real_data_smoke_closes_the_semantic_loop(tmp_path: Path) -> None:
    """Exercise the same public APIs as the local 100K smoke on two-row data."""

    repository_root = Path(__file__).resolve().parents[2]
    shutil.copytree(repository_root / "examples/real_data", tmp_path / "examples/real_data")
    bts_path = tmp_path / "data/processed/bts/on_time/2024-01/bts_flights_100000.parquet"
    nyc_path = tmp_path / "data/processed/nyc_tlc/yellow/2024-01/yellow_taxi_100000.parquet"
    zone_path = tmp_path / "data/processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet"

    connection = duckdb.connect()
    try:
        _write_parquet(
            connection,
            bts_path,
            "SELECT * FROM (VALUES "
            "('N123AA', DATE '2024-01-10', 'JFK', 'LAX', 2475.0, false), "
            "('N456BB', DATE '2024-01-11', 'JFK', 'BOS', 187.0, false)) "
            "AS t(Tail_Number, FlightDate, Origin, Dest, Distance, Cancelled)",
        )
        _write_parquet(
            connection,
            nyc_path,
            "SELECT * FROM (VALUES "
            "(1, TIMESTAMP '2024-01-10 12:00:00', 3.0, 20.0), "
            "(2, TIMESTAMP '2024-01-11 12:00:00', 1.0, 5.0)) "
            "AS t(PULocationID, tpep_pickup_datetime, trip_distance, total_amount)",
        )
        _write_parquet(
            connection,
            zone_path,
            "SELECT * FROM (VALUES "
            "(1, 'Manhattan', 'Yellow Zone'), (2, 'Queens', 'Boro Zone')) "
            "AS t(LocationID, Borough, service_zone)",
        )
    finally:
        connection.close()

    _write_slice_manifest(tmp_path / "data", 100_000, 2)

    payload = run_governed_real_data_smoke(tmp_path)

    assert payload["status"] == "PASS"
    assert [case["row_count"] for case in payload["governed_cases"]] == [1, 1]
    assert payload["governed_cases"][0]["raw_sensitive_exposure_rows"] == 0
    assert all(case["certificate_status"] == "PARTIAL" for case in payload["governed_cases"])
    assert all(
        case["actual_status"] == case["expected_status"] for case in payload["negative_cases"]
    )

    candidates = run_real_data_candidate_smoke(tmp_path)
    assert candidates["status"] == "PASS"
    assert all(item["candidate_count"] == 3 for item in candidates["workloads"])
    assert all(item["distinct_duckdb_plan_count"] == 3 for item in candidates["workloads"])
    assert all(item["strict_profile_rejected_raw_boundary"] for item in candidates["workloads"])


def test_real_data_pilot_writes_resumable_audit_artifacts(tmp_path: Path) -> None:
    """A completed atomic unit is preserved and skipped on safe resume."""

    repository_root = Path(__file__).resolve().parents[2]
    shutil.copytree(repository_root / "examples/real_data", tmp_path / "examples/real_data")
    connection = duckdb.connect()
    try:
        _write_parquet(
            connection,
            tmp_path / "data/processed/bts/on_time/2024-01/bts_flights_2.parquet",
            "SELECT * FROM (VALUES "
            "('N123AA', DATE '2024-01-10', 'JFK', 'LAX', 2475.0, false), "
            "('N456BB', DATE '2024-01-11', 'JFK', 'BOS', 187.0, false)) "
            "AS t(Tail_Number, FlightDate, Origin, Dest, Distance, Cancelled)",
        )
        _write_parquet(
            connection,
            tmp_path / "data/processed/nyc_tlc/yellow/2024-01/yellow_taxi_2.parquet",
            "SELECT * FROM (VALUES "
            "(1, TIMESTAMP '2024-01-10 12:00:00', 3.0, 20.0)) "
            "AS t(PULocationID, tpep_pickup_datetime, trip_distance, total_amount)",
        )
        _write_parquet(
            connection,
            tmp_path / "data/processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet",
            "SELECT 1::BIGINT AS LocationID, 'Manhattan'::VARCHAR AS Borough, "
            "'Yellow Zone'::VARCHAR AS service_zone",
        )
    finally:
        connection.close()

    _write_slice_manifest(tmp_path / "data", 2, 1)

    config = RealDataPilotConfig(
        results_dir="results/pilot-test",
        workloads=("bts",),
        sample_rows=(2,),
        warmup_runs=0,
        measured_runs=1,
        duckdb_threads=1,
        duckdb_memory_limit_mb=512,
    )
    run_dir = run_real_data_pilot(config, project_root=tmp_path)
    unit_path = run_dir / "units/bts-n2.json"
    original_unit = unit_path.read_text(encoding="utf-8")

    resumed = run_real_data_pilot(
        config,
        project_root=tmp_path,
        resume_run_id=run_dir.name,
    )

    assert resumed == run_dir
    assert unit_path.read_text(encoding="utf-8") == original_unit
    assert (run_dir / "measurements.csv").is_file()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["paper_performance_evidence"] is False
    assert summary["completed_units"] == summary["expected_units"] == 1
    acceptance = analyze_real_data_pilot(run_dir)
    assert acceptance["status"] == "PASS"
    assert (run_dir / "acceptance.json").is_file()
    assert (run_dir / "report.md").is_file()

    candidate_config = RealDataCandidatePilotConfig(
        results_dir="results/candidate-pilot-test",
        workloads=("bts",),
        sample_rows=(2,),
        warmup_runs=0,
        measured_runs=3,
        duckdb_threads=1,
        duckdb_memory_limit_mb=512,
    )
    candidate_run = run_real_data_candidate_pilot(
        candidate_config,
        project_root=tmp_path,
    )
    candidate_acceptance = analyze_real_data_candidate_pilot(candidate_run)
    assert candidate_acceptance["status"] == "PASS"
    assert candidate_acceptance["full_month_preexperiment_authorized"] is True
