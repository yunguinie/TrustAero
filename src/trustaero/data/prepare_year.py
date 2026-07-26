"""Resumable preparation of the frozen 2024 BTS and NYC monthly workload.

The January smoke pipeline remains unchanged. This module prepares the
February--December evaluation inputs after the downloader has verified each
official file. A month-level manifest is published only after every derived
BTS Parquet file is complete, so an interrupted run can safely resume without
silently accepting partial output.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file
from trustaero.data.prepare import (
    _BTS_COLUMNS,
    PreparationError,
    PreparedArtifact,
    _atomic_json,
    _bts_csv_member,
    _copy_query_to_parquet,
    _parquet_record,
    _quote_identifier,
    _require_under_root,
    _sql_literal,
)

YearStageCallback = Callable[[str], None]


def normalize_2024_months(months: Iterable[int]) -> tuple[int, ...]:
    """Return unique ordered months and reject values outside calendar 2024."""

    values = tuple(sorted(set(months)))
    if not values or any(month < 1 or month > 12 for month in values):
        raise PreparationError("months must contain integers from 1 through 12")
    return values


def _download_record(data_root: Path, artifact_id: str, raw_path: Path) -> dict[str, Any]:
    """Re-verify a downloader audit before allowing an official file into ETL."""

    audit_path = data_root / "manifests/downloads" / f"{artifact_id}.json"
    if not audit_path.is_file() or not raw_path.is_file():
        raise PreparationError(f"Verified download is missing: {artifact_id}")
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"Download audit is unreadable: {artifact_id}") from exc
    relative = raw_path.relative_to(data_root.parent).as_posix()
    observed_size = raw_path.stat().st_size
    observed_digest = sha256_file(raw_path)
    if (
        audit.get("local_path") != relative
        or int(audit.get("byte_size", -1)) != observed_size
        or audit.get("sha256") != observed_digest
    ):
        raise PreparationError(f"Downloaded artifact changed after audit: {artifact_id}")
    return {
        "artifact_id": artifact_id,
        "relative_path": raw_path.relative_to(data_root).as_posix(),
        "byte_size": observed_size,
        "sha256": observed_digest,
    }


def _month_paths(data_root: Path, month: int) -> tuple[Path, Path, Path]:
    suffix = f"{month:02d}"
    bts_zip = data_root / (
        "raw/bts/on_time/2024/"
        f"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_2024_{month}.zip"
    )
    nyc = data_root / f"raw/nyc_tlc/yellow/2024/yellow_tripdata_2024-{suffix}.parquet"
    output = data_root / f"processed/bts/on_time/2024-{suffix}"
    return bts_zip, nyc, output


def _manifest_is_reusable(
    path: Path,
    *,
    data_root: Path,
    inputs: list[dict[str, Any]],
) -> bool:
    """Accept an existing month only when every frozen input and output still matches."""

    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("inputs") != inputs or payload.get("status") != "PASS":
            return False
        outputs = payload.get("outputs")
        if not isinstance(outputs, list) or len(outputs) != 3:
            return False
        for item in outputs:
            output = _require_under_root(data_root / str(item["relative_path"]), data_root)
            if (
                not output.is_file()
                or output.stat().st_size != int(item["byte_size"])
                or sha256_file(output) != item["sha256"]
            ):
                return False
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _prepare_bts_month(
    connection: Any,
    *,
    data_root: Path,
    month: int,
    bts_zip: Path,
    output_dir: Path,
    stage: YearStageCallback | None,
) -> list[PreparedArtifact]:
    """Convert one verified BTS ZIP and derive its natural dimensions."""

    suffix = f"{month:02d}"
    scratch = _require_under_root(data_root / "tmp/real-data-2024", data_root)
    scratch.mkdir(parents=True, exist_ok=True)
    csv_path = scratch / f"bts_on_time_2024_{suffix}.csv"
    if stage is not None:
        stage(f"month {suffix}: validating ZIP CRC and extracting the BTS CSV")
    try:
        with zipfile.ZipFile(bts_zip) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise PreparationError(f"BTS 2024-{suffix} ZIP CRC failed at {bad_member}")
            member = _bts_csv_member(archive)
            with archive.open(member) as archive_source, csv_path.open("wb") as destination:
                shutil.copyfileobj(archive_source, destination, length=1024 * 1024)

        if stage is not None:
            stage(f"month {suffix}: converting 40 reviewed BTS fields to Parquet")
        quoted_columns = ", ".join(_quote_identifier(name) for name in _BTS_COLUMNS)
        csv_source = (
            f"read_csv_auto({_sql_literal(csv_path)}, header=true, sample_size=-1, "
            "null_padding=true)"
        )
        fact = output_dir / "bts_flights_full.parquet"
        _copy_query_to_parquet(
            connection,
            f"SELECT {quoted_columns} FROM {csv_source}",
            fact,
        )
        artifacts = [
            _parquet_record(
                connection,
                artifact_id=f"bts_on_time_2024_{suffix}_full",
                path=fact,
                data_root=data_root,
                derivation="selected 40 reviewed fields from the official monthly CSV",
            )
        ]
        bts_source = f"read_parquet({_sql_literal(fact)})"
        airports = output_dir / "bts_airports.parquet"
        _copy_query_to_parquet(
            connection,
            "SELECT DISTINCT airport_id, airport_code, city_name, state_code FROM ("
            f"SELECT OriginAirportID AS airport_id, Origin AS airport_code, "
            f"OriginCityName AS city_name, OriginState AS state_code FROM {bts_source} "
            "UNION ALL "
            f"SELECT DestAirportID, Dest, DestCityName, DestState FROM {bts_source}"
            ") ORDER BY airport_id",
            airports,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id=f"bts_on_time_2024_{suffix}_airports",
                path=airports,
                data_root=data_root,
                derivation="distinct observed monthly origin/destination airports",
            )
        )
        carriers = output_dir / "bts_carriers.parquet"
        _copy_query_to_parquet(
            connection,
            "SELECT DISTINCT DOT_ID_Reporting_Airline AS carrier_id, "
            f"Reporting_Airline AS carrier_code FROM {bts_source} ORDER BY carrier_id",
            carriers,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id=f"bts_on_time_2024_{suffix}_carriers",
                path=carriers,
                data_root=data_root,
                derivation="distinct observed monthly reporting carriers",
            )
        )
        return artifacts
    finally:
        if csv_path.exists():
            csv_path.unlink()


def prepare_real_data_2024(
    data_root: Path,
    *,
    months: Iterable[int] = range(2, 13),
    stage: YearStageCallback | None = None,
) -> dict[str, Any]:
    """Prepare verified monthly inputs with safe month-level resume semantics."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional environment
        raise PreparationError("DuckDB is required for annual preparation") from exc

    selected = normalize_2024_months(months)
    root = data_root.resolve()
    (root / "tmp/duckdb-year").mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    month_records: list[dict[str, Any]] = []
    try:
        connection.execute("SET memory_limit = '4GB'")
        connection.execute(f"SET temp_directory = {_sql_literal(root / 'tmp/duckdb-year')}")
        connection.execute("SET preserve_insertion_order = true")
        for index, month in enumerate(selected, start=1):
            suffix = f"{month:02d}"
            bts_zip, nyc_raw, output_dir = _month_paths(root, month)
            bts_input = _download_record(root, f"bts_on_time_2024_{suffix}", bts_zip)
            nyc_input = _download_record(root, f"nyc_tlc_yellow_2024_{suffix}", nyc_raw)
            inputs = [bts_input, nyc_input]
            month_manifest = root / f"manifests/processed/real-data-2024-{suffix}.json"
            if _manifest_is_reusable(month_manifest, data_root=root, inputs=inputs):
                payload = json.loads(month_manifest.read_text(encoding="utf-8"))
                month_records.append(payload)
                if stage is not None:
                    stage(f"[{index}/{len(selected)}] month {suffix}: verified existing outputs")
                continue

            if stage is not None:
                stage(f"[{index}/{len(selected)}] month {suffix}: preparing")
            outputs = _prepare_bts_month(
                connection,
                data_root=root,
                month=month,
                bts_zip=bts_zip,
                output_dir=output_dir,
                stage=stage,
            )
            nyc_source = f"read_parquet({_sql_literal(nyc_raw)})"
            nyc_row = connection.execute(f"SELECT count(*) FROM {nyc_source}").fetchone()
            if nyc_row is None:
                raise PreparationError(f"NYC 2024-{suffix} row count is missing")
            nyc_schema = [
                [str(row[0]), str(row[1])]
                for row in connection.execute(f"DESCRIBE SELECT * FROM {nyc_source}").fetchall()
            ]
            payload = {
                "schema_version": 1,
                "status": "PASS",
                "year": 2024,
                "month": month,
                "prepared_at_utc": datetime.now(UTC).isoformat(),
                "purpose": "official monthly real-data input; no performance result",
                "inputs": inputs,
                "nyc_row_count": int(nyc_row[0]),
                "nyc_schema": nyc_schema,
                "outputs": [asdict(item) for item in outputs],
            }
            _atomic_json(month_manifest, payload)
            month_records.append(payload)
            if stage is not None:
                stage(f"[{index}/{len(selected)}] month {suffix}: complete")
    finally:
        connection.close()

    aggregate = {
        "schema_version": 1,
        "status": "PASS",
        "year": 2024,
        "months": list(selected),
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "month_count": len(month_records),
        "bts_total_rows": sum(int(record["outputs"][0]["row_count"]) for record in month_records),
        "nyc_total_rows": sum(int(record["nyc_row_count"]) for record in month_records),
        "month_manifest_paths": [
            f"manifests/processed/real-data-2024-{month:02d}.json" for month in selected
        ],
        "scientific_boundary": (
            "Preparation verifies official rows and deterministic BTS transformations; "
            "it does not authorize or evaluate an optimizer."
        ),
    }
    _atomic_json(root / "manifests/processed/real-data-2024-main.json", aggregate)
    return aggregate
