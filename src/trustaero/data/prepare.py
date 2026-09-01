"""Deterministic preparation of the approved real-data smoke workloads.

This module is intentionally separate from TrustAero's validator and
optimizer. It treats official downloads as immutable inputs, creates derived
Parquet files atomically, and records exact schemas, row counts, byte sizes,
and checksums for later reproduction.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file

StageCallback = Callable[[str], None]


class PreparationError(RuntimeError):
    """Raised when an input or derived artifact fails reproducibility checks."""


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    """Auditable properties of one derived Parquet artifact."""

    artifact_id: str
    relative_path: str
    row_count: int
    byte_size: int
    sha256: str
    schema: tuple[tuple[str, str], ...]
    derivation: str


_BTS_COLUMNS = (
    "Year",
    "Quarter",
    "Month",
    "DayofMonth",
    "DayOfWeek",
    "FlightDate",
    "Reporting_Airline",
    "DOT_ID_Reporting_Airline",
    "Tail_Number",
    "Flight_Number_Reporting_Airline",
    "OriginAirportID",
    "Origin",
    "OriginCityName",
    "OriginState",
    "DestAirportID",
    "Dest",
    "DestCityName",
    "DestState",
    "CRSDepTime",
    "DepTime",
    "DepDelayMinutes",
    "DepDel15",
    "DepartureDelayGroups",
    "CRSArrTime",
    "ArrTime",
    "ArrDelayMinutes",
    "ArrDel15",
    "ArrivalDelayGroups",
    "Cancelled",
    "CancellationCode",
    "Diverted",
    "CRSElapsedTime",
    "ActualElapsedTime",
    "AirTime",
    "Distance",
    "CarrierDelay",
    "WeatherDelay",
    "NASDelay",
    "SecurityDelay",
    "LateAircraftDelay",
)


def _sql_literal(value: Path | str) -> str:
    """Quote an internal string as one DuckDB SQL literal."""

    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _require_under_root(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PreparationError(f"Path escapes data root: {path}") from exc
    return resolved_path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def _copy_query_to_parquet(connection: Any, query: str, output: Path) -> None:
    """Materialize a query without ever exposing a partial final Parquet file."""

    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(f"{output.stem}.part{output.suffix}")
    if part.exists():
        part.unlink()
    try:
        connection.execute(
            f"COPY ({query}) TO {_sql_literal(part)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        os.replace(part, output)
    finally:
        if part.exists():
            part.unlink()


def _parquet_record(
    connection: Any,
    *,
    artifact_id: str,
    path: Path,
    data_root: Path,
    derivation: str,
) -> PreparedArtifact:
    source = f"read_parquet({_sql_literal(path)})"
    row_count = int(connection.execute(f"SELECT count(*) FROM {source}").fetchone()[0])
    schema = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    )
    return PreparedArtifact(
        artifact_id=artifact_id,
        relative_path=path.relative_to(data_root).as_posix(),
        row_count=row_count,
        byte_size=path.stat().st_size,
        sha256=sha256_file(path),
        schema=schema,
        derivation=derivation,
    )


def _bts_csv_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
    if len(members) != 1:
        raise PreparationError(f"Expected exactly one BTS CSV member, found {len(members)}")
    return members[0]


def _validate_slice_sizes(slice_sizes: Iterable[int]) -> tuple[int, ...]:
    values = tuple(sorted(set(slice_sizes)))
    if not values or any(value <= 0 for value in values):
        raise PreparationError("Slice sizes must contain positive integers")
    return values


def _even_source_order_sample(source: str, *, total_rows: int, sample_rows: int) -> str:
    """Select exact, evenly spaced row positions from an immutable source order.

    Prefix slices can accidentally cover only a narrow time interval. This
    deterministic position sample spans the complete fixed file without using
    random seeds or depending on data values that might be null or duplicated.
    """

    return (
        "SELECT s.* EXCLUDE (_source_row_number) FROM ("
        f"SELECT *, row_number() OVER () AS _source_row_number FROM {source}"
        ") s JOIN ("
        f"SELECT CAST(floor(i * {total_rows} / {sample_rows}) AS BIGINT) + 1 "
        f"AS _source_row_number FROM range({sample_rows}) AS positions(i)"
        ") p USING (_source_row_number) ORDER BY _source_row_number"
    )


def prepare_real_data_smoke(
    data_root: Path,
    *,
    slice_sizes: Iterable[int] = (100_000, 500_000),
    stage: StageCallback | None = None,
) -> dict[str, Any]:
    """Prepare BTS and NYC January integration artifacts and return a manifest."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise PreparationError("DuckDB is required; install the project duckdb extra") from exc

    sizes = _validate_slice_sizes(slice_sizes)
    root = data_root.resolve()
    bts_zip = _require_under_root(
        root / "raw/bts/on_time/2024/"
        "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_2024_1.zip",
        root,
    )
    nyc_raw = _require_under_root(
        root / "raw/nyc_tlc/yellow/2024/yellow_tripdata_2024-01.parquet", root
    )
    zone_csv = _require_under_root(root / "raw/nyc_tlc/lookup/taxi_zone_lookup.csv", root)
    for required in (bts_zip, nyc_raw, zone_csv):
        if not required.is_file():
            raise PreparationError(f"Required smoke input is missing: {required}")

    tmp_dir = _require_under_root(root / "tmp/real-data-smoke", root)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    bts_csv = tmp_dir / "bts_on_time_2024_01.csv"
    bts_out_dir = root / "processed/bts/on_time/2024-01"
    nyc_out_dir = root / "processed/nyc_tlc/yellow/2024-01"

    if stage is not None:
        stage("Validating BTS ZIP CRC and extracting its single CSV to E-drive scratch space")
    with zipfile.ZipFile(bts_zip) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise PreparationError(f"BTS ZIP CRC validation failed at member: {bad_member}")
        member = _bts_csv_member(archive)
        with archive.open(member) as source, bts_csv.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)

    connection = duckdb.connect()
    artifacts: list[PreparedArtifact] = []
    try:
        connection.execute("SET memory_limit = '4GB'")
        connection.execute(f"SET temp_directory = {_sql_literal(root / 'tmp/duckdb')}")
        connection.execute("SET preserve_insertion_order = true")

        if stage is not None:
            stage("Converting selected BTS columns to compressed Parquet")
        quoted_columns = ", ".join(_quote_identifier(name) for name in _BTS_COLUMNS)
        bts_csv_source = (
            f"read_csv_auto({_sql_literal(bts_csv)}, header=true, sample_size=-1, "
            "null_padding=true)"
        )
        bts_full = bts_out_dir / "bts_flights_full.parquet"
        _copy_query_to_parquet(
            connection,
            f"SELECT {quoted_columns} FROM {bts_csv_source}",
            bts_full,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id="bts_on_time_2024_01_full",
                path=bts_full,
                data_root=root,
                derivation="selected 40 documented fields from the official January CSV",
            )
        )

        if stage is not None:
            stage("Building BTS airport and carrier dimensions from observed identifiers")
        bts_source = f"read_parquet({_sql_literal(bts_full)})"
        airport_dimension = bts_out_dir / "bts_airports.parquet"
        _copy_query_to_parquet(
            connection,
            "SELECT DISTINCT airport_id, airport_code, city_name, state_code FROM ("
            f"SELECT OriginAirportID AS airport_id, Origin AS airport_code, "
            f"OriginCityName AS city_name, OriginState AS state_code FROM {bts_source} "
            "UNION ALL "
            f"SELECT DestAirportID, Dest, DestCityName, DestState FROM {bts_source}"
            ") ORDER BY airport_id",
            airport_dimension,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id="bts_on_time_2024_01_airports",
                path=airport_dimension,
                data_root=root,
                derivation="distinct observed origin/destination airport identifiers",
            )
        )
        carrier_dimension = bts_out_dir / "bts_carriers.parquet"
        _copy_query_to_parquet(
            connection,
            f"SELECT DISTINCT DOT_ID_Reporting_Airline AS carrier_id, "
            f"Reporting_Airline AS carrier_code FROM {bts_source} ORDER BY carrier_id",
            carrier_dimension,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id="bts_on_time_2024_01_carriers",
                path=carrier_dimension,
                data_root=root,
                derivation="distinct observed reporting-carrier identifiers",
            )
        )

        if stage is not None:
            stage("Creating deterministic BTS and NYC samples spanning each source file")
        bts_count = artifacts[0].row_count
        nyc_source = f"read_parquet({_sql_literal(nyc_raw)})"
        nyc_count_row = connection.execute(f"SELECT count(*) FROM {nyc_source}").fetchone()
        if nyc_count_row is None:
            raise PreparationError("DuckDB returned no NYC row-count result")
        nyc_count = int(nyc_count_row[0])
        for size in sizes:
            if size > bts_count or size > nyc_count:
                raise PreparationError(
                    f"Slice {size} exceeds BTS ({bts_count}) or NYC ({nyc_count}) rows"
                )
            bts_slice = bts_out_dir / f"bts_flights_{size}.parquet"
            _copy_query_to_parquet(
                connection,
                _even_source_order_sample(bts_source, total_rows=bts_count, sample_rows=size),
                bts_slice,
            )
            artifacts.append(
                _parquet_record(
                    connection,
                    artifact_id=f"bts_on_time_2024_01_{size}",
                    path=bts_slice,
                    data_root=root,
                    derivation=(
                        f"{size} evenly spaced positions across immutable official CSV order; "
                        "smoke only"
                    ),
                )
            )

            nyc_slice = nyc_out_dir / f"yellow_taxi_{size}.parquet"
            _copy_query_to_parquet(
                connection,
                _even_source_order_sample(nyc_source, total_rows=nyc_count, sample_rows=size),
                nyc_slice,
            )
            artifacts.append(
                _parquet_record(
                    connection,
                    artifact_id=f"nyc_tlc_yellow_2024_01_{size}",
                    path=nyc_slice,
                    data_root=root,
                    derivation=(
                        f"{size} evenly spaced positions across immutable Parquet order; smoke only"
                    ),
                )
            )

        zone_parquet = nyc_out_dir / "taxi_zones.parquet"
        zone_source = f"read_csv_auto({_sql_literal(zone_csv)}, header=true, sample_size=-1)"
        _copy_query_to_parquet(connection, f"SELECT * FROM {zone_source}", zone_parquet)
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id="nyc_tlc_taxi_zones",
                path=zone_parquet,
                data_root=root,
                derivation="lossless conversion of official Taxi Zone Lookup CSV",
            )
        )
    finally:
        connection.close()
        if bts_csv.exists():
            bts_csv.unlink()

    manifest = {
        "schema_version": 1,
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "integration smoke only; not a paper performance result",
        "slice_policy": (
            "deterministic evenly spaced source-order positions spanning the fixed file; "
            "final experiments use full months"
        ),
        "inputs": [
            {
                "artifact_id": "bts_on_time_2024_01",
                "relative_path": bts_zip.relative_to(root).as_posix(),
                "byte_size": bts_zip.stat().st_size,
                "sha256": sha256_file(bts_zip),
            },
            {
                "artifact_id": "nyc_tlc_yellow_2024_01",
                "relative_path": nyc_raw.relative_to(root).as_posix(),
                "byte_size": nyc_raw.stat().st_size,
                "sha256": sha256_file(nyc_raw),
            },
            {
                "artifact_id": "nyc_tlc_taxi_zone_lookup",
                "relative_path": zone_csv.relative_to(root).as_posix(),
                "byte_size": zone_csv.stat().st_size,
                "sha256": sha256_file(zone_csv),
            },
        ],
        "outputs": [asdict(artifact) for artifact in artifacts],
    }
    manifest_path = root / "manifests/processed/real-data-smoke.json"
    _atomic_json(manifest_path, manifest)
    if stage is not None:
        stage(f"Wrote processed-data audit manifest: {manifest_path}")
    return manifest
