"""Resumable preparation of an official BTS calendar-year workload.

This module is intentionally BTS-only.  It keeps a new temporal holdout
separate from the frozen 2024 BTS/NYC experiment and publishes a month-level
manifest only after all three derived Parquet artifacts have been verified.
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

BtsYearStageCallback = Callable[[str], None]


def normalize_calendar_months(months: Iterable[int]) -> tuple[int, ...]:
    """Return unique ordered calendar months and reject invalid values."""

    values = tuple(sorted(set(months)))
    if not values or any(month < 1 or month > 12 for month in values):
        raise PreparationError("months must contain integers from 1 through 12")
    return values


def _paths(data_root: Path, year: int, month: int) -> tuple[Path, Path]:
    filename = f"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
    return (
        data_root / f"raw/bts/on_time/{year}/{filename}",
        data_root / f"processed/bts/on_time/{year}-{month:02d}",
    )


def _download_record(data_root: Path, year: int, month: int, raw_path: Path) -> dict[str, Any]:
    artifact_id = f"bts_on_time_{year}_{month:02d}"
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
        "source_url": str(audit.get("artifact", {}).get("url", "")),
        "relative_path": raw_path.relative_to(data_root).as_posix(),
        "byte_size": observed_size,
        "sha256": observed_digest,
    }


def _manifest_is_reusable(
    path: Path,
    *,
    data_root: Path,
    expected_input: dict[str, Any],
) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "PASS" or payload.get("inputs") != [expected_input]:
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


def _prepare_month(
    connection: Any,
    *,
    data_root: Path,
    year: int,
    month: int,
    bts_zip: Path,
    output_dir: Path,
    stage: BtsYearStageCallback | None,
) -> list[PreparedArtifact]:
    suffix = f"{month:02d}"
    scratch = _require_under_root(data_root / f"tmp/bts-{year}", data_root)
    scratch.mkdir(parents=True, exist_ok=True)
    csv_path = scratch / f"bts_on_time_{year}_{suffix}.csv"
    try:
        if stage is not None:
            stage(f"{year}-{suffix}: validating ZIP CRC and extracting CSV")
        with zipfile.ZipFile(bts_zip) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise PreparationError(f"BTS {year}-{suffix} ZIP CRC failed at {bad_member}")
            member = _bts_csv_member(archive)
            with archive.open(member) as archive_source, csv_path.open("wb") as destination:
                shutil.copyfileobj(archive_source, destination, length=1024 * 1024)

        if stage is not None:
            stage(f"{year}-{suffix}: converting reviewed BTS fields to Parquet")
        quoted = ", ".join(_quote_identifier(name) for name in _BTS_COLUMNS)
        csv_source = (
            f"read_csv_auto({_sql_literal(csv_path)}, header=true, sample_size=-1, "
            "null_padding=true)"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        fact = output_dir / "bts_flights_full.parquet"
        _copy_query_to_parquet(connection, f"SELECT {quoted} FROM {csv_source}", fact)
        artifacts = [
            _parquet_record(
                connection,
                artifact_id=f"bts_on_time_{year}_{suffix}_full",
                path=fact,
                data_root=data_root,
                derivation="selected 40 reviewed fields from the official monthly CSV",
            )
        ]
        parquet_source = f"read_parquet({_sql_literal(fact)})"
        airports = output_dir / "bts_airports.parquet"
        _copy_query_to_parquet(
            connection,
            "SELECT DISTINCT airport_id, airport_code, city_name, state_code FROM ("
            f"SELECT OriginAirportID AS airport_id, Origin AS airport_code, "
            f"OriginCityName AS city_name, OriginState AS state_code FROM {parquet_source} "
            "UNION ALL "
            f"SELECT DestAirportID, Dest, DestCityName, DestState FROM {parquet_source}"
            ") ORDER BY airport_id",
            airports,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id=f"bts_on_time_{year}_{suffix}_airports",
                path=airports,
                data_root=data_root,
                derivation="distinct observed monthly origin/destination airports",
            )
        )
        carriers = output_dir / "bts_carriers.parquet"
        _copy_query_to_parquet(
            connection,
            "SELECT DISTINCT DOT_ID_Reporting_Airline AS carrier_id, "
            f"Reporting_Airline AS carrier_code FROM {parquet_source} ORDER BY carrier_id",
            carriers,
        )
        artifacts.append(
            _parquet_record(
                connection,
                artifact_id=f"bts_on_time_{year}_{suffix}_carriers",
                path=carriers,
                data_root=data_root,
                derivation="distinct observed monthly reporting carriers",
            )
        )
        return artifacts
    finally:
        if csv_path.exists():
            csv_path.unlink()


def prepare_bts_calendar_year(
    data_root: Path,
    *,
    year: int,
    months: Iterable[int] = range(1, 13),
    stage: BtsYearStageCallback | None = None,
) -> dict[str, Any]:
    """Prepare verified BTS months with month-level resumability."""

    if year < 1987:
        raise PreparationError("BTS on-time data begin in 1987")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise PreparationError("DuckDB is required for annual BTS preparation") from exc

    selected = normalize_calendar_months(months)
    root = data_root.resolve()
    temp = root / f"tmp/duckdb-bts-{year}"
    temp.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    records: list[dict[str, Any]] = []
    try:
        connection.execute("SET memory_limit = '4GB'")
        connection.execute(f"SET temp_directory = {_sql_literal(temp)}")
        connection.execute("SET preserve_insertion_order = true")
        for index, month in enumerate(selected, start=1):
            raw, output = _paths(root, year, month)
            input_record = _download_record(root, year, month, raw)
            manifest = root / f"manifests/processed/bts-{year}-{month:02d}.json"
            if _manifest_is_reusable(
                manifest,
                data_root=root,
                expected_input=input_record,
            ):
                records.append(json.loads(manifest.read_text(encoding="utf-8")))
                if stage is not None:
                    stage(
                        f"[{index}/{len(selected)}] {year}-{month:02d}: verified existing outputs"
                    )
                continue
            outputs = _prepare_month(
                connection,
                data_root=root,
                year=year,
                month=month,
                bts_zip=raw,
                output_dir=output,
                stage=stage,
            )
            payload = {
                "schema_version": 1,
                "status": "PASS",
                "year": year,
                "month": month,
                "prepared_at_utc": datetime.now(UTC).isoformat(),
                "purpose": "out-of-time official BTS candidate-family evaluation input",
                "inputs": [input_record],
                "outputs": [asdict(item) for item in outputs],
            }
            _atomic_json(manifest, payload)
            records.append(payload)
            if stage is not None:
                stage(f"[{index}/{len(selected)}] {year}-{month:02d}: complete")
    finally:
        connection.close()

    aggregate = {
        "schema_version": 1,
        "status": "PASS",
        "year": year,
        "months": list(selected),
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "month_count": len(records),
        "bts_total_rows": sum(int(record["outputs"][0]["row_count"]) for record in records),
        "month_manifest_paths": [
            f"manifests/processed/bts-{year}-{month:02d}.json" for month in selected
        ],
        "scientific_boundary": (
            "Preparation verifies official downloads and deterministic transformations; "
            "it does not inspect or authorize a performance conclusion."
        ),
    }
    _atomic_json(root / f"manifests/processed/bts-{year}-main.json", aggregate)
    return aggregate
