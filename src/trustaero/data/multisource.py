"""Deterministic preparation for the four-source end-to-end case study.

The raw files intentionally remain immutable.  This module verifies the
download audits, normalizes the four publisher-specific schemas, filters every
source to the frozen New York State scope, and records the exact derived
Parquet files.  It does not execute TrustAero queries or authorize paper
claims; those are separate gates.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file

StageCallback = Callable[[str], None]

_MIN_LATITUDE = 40.4774
_MAX_LATITUDE = 45.0153
_MIN_LONGITUDE = -79.7624
_MAX_LONGITUDE = -71.7517

_RAW_ARTIFACTS = {
    "multisource_usgs_earthquakes_ny_2000_2024": Path(
        "raw/multisource/usgs/earthquakes_ny_2000_2024.csv"
    ),
    "multisource_nysdec_regulated_wells_20260724": Path(
        "raw/multisource/nysdec/regulated_wells_20260724.csv"
    ),
    "multisource_faa_airports_20241128": Path("raw/multisource/faa/28_Nov_2024_APT_CSV.zip"),
    "multisource_census_places_2024": Path("raw/multisource/census/2024_Gaz_place_national.zip"),
}


class MultisourcePreparationError(RuntimeError):
    """Raised when source binding or deterministic preparation fails."""


@dataclass(frozen=True, slots=True)
class MultisourcePreparedArtifact:
    """Auditable properties of one normalized case-study table."""

    artifact_id: str
    relative_path: str
    source_rows: int
    row_count: int
    dropped_rows: int
    byte_size: int
    sha256: str
    schema: tuple[tuple[str, str], ...]
    derivation: str


def _sql_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish a complete manifest atomically, never a partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def _validate_download_audit(
    data_root: Path, artifact_id: str, relative_path: Path
) -> dict[str, Any]:
    """Bind one raw file to the audit created by the resumable downloader."""

    audit_path = data_root / "manifests" / "downloads" / f"{artifact_id}.json"
    raw_path = data_root / relative_path
    if not audit_path.is_file() or not raw_path.is_file():
        raise MultisourcePreparationError(f"Missing raw file or download audit for {artifact_id}")

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MultisourcePreparationError(
            f"Cannot read download audit for {artifact_id}: {exc}"
        ) from exc

    expected_local_path = f"data/{relative_path.as_posix()}"
    byte_size = raw_path.stat().st_size
    digest = sha256_file(raw_path)
    if (
        audit.get("artifact", {}).get("artifact_id") != artifact_id
        or audit.get("local_path") != expected_local_path
        or audit.get("byte_size") != byte_size
        or audit.get("sha256") != digest
    ):
        raise MultisourcePreparationError(
            f"Raw file no longer matches its download audit: {artifact_id}"
        )
    return {
        "artifact_id": artifact_id,
        "relative_path": relative_path.as_posix(),
        "byte_size": byte_size,
        "sha256": digest,
        "download_audit": audit_path.relative_to(data_root).as_posix(),
    }


def _extract_member(archive_path: Path, member_name: str, destination: Path) -> None:
    """Extract one expected member and reject ambiguous or unsafe ZIP layouts."""

    with zipfile.ZipFile(archive_path) as archive:
        matches = [item for item in archive.infolist() if item.filename == member_name]
        if len(matches) != 1 or matches[0].is_dir():
            raise MultisourcePreparationError(
                f"Expected exactly one {member_name} in {archive_path.name}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(matches[0]) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _copy_query_to_parquet(connection: Any, query: str, output: Path) -> None:
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


def _source_count(connection: Any, source: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {source}").fetchone()[0])


def _prepared_record(
    connection: Any,
    *,
    artifact_id: str,
    output: Path,
    data_root: Path,
    source_rows: int,
    derivation: str,
) -> MultisourcePreparedArtifact:
    source = f"read_parquet({_sql_literal(output)})"
    row_count = _source_count(connection, source)
    schema = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    )
    return MultisourcePreparedArtifact(
        artifact_id=artifact_id,
        relative_path=output.relative_to(data_root).as_posix(),
        source_rows=source_rows,
        row_count=row_count,
        dropped_rows=source_rows - row_count,
        byte_size=output.stat().st_size,
        sha256=sha256_file(output),
        schema=schema,
        derivation=derivation,
    )


def prepare_multisource_case(
    data_root: Path,
    *,
    stage: StageCallback | None = None,
) -> dict[str, Any]:
    """Normalize the four frozen sources and write one bound preparation manifest."""

    notify = stage or (lambda _message: None)
    data_root = data_root.resolve()
    raw_inputs = [
        _validate_download_audit(data_root, artifact_id, relative_path)
        for artifact_id, relative_path in _RAW_ARTIFACTS.items()
    ]

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MultisourcePreparationError("DuckDB is required; run inside TrustAero_env.") from exc

    output_dir = data_root / "processed" / "multisource" / "v1"
    staging = output_dir / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    faa_csv = staging / "APT_BASE.csv"
    census_tsv = staging / "2024_Gaz_place_national.txt"
    _extract_member(
        data_root / _RAW_ARTIFACTS["multisource_faa_airports_20241128"],
        "APT_BASE.csv",
        faa_csv,
    )
    _extract_member(
        data_root / _RAW_ARTIFACTS["multisource_census_places_2024"],
        "2024_Gaz_place_national.txt",
        census_tsv,
    )

    earthquake_csv = data_root / _RAW_ARTIFACTS["multisource_usgs_earthquakes_ny_2000_2024"]
    well_csv = data_root / _RAW_ARTIFACTS["multisource_nysdec_regulated_wells_20260724"]
    connection = duckdb.connect()
    outputs: list[MultisourcePreparedArtifact] = []
    try:
        sources = {
            "earthquakes": f"read_csv_auto({_sql_literal(earthquake_csv)}, header=true)",
            "wells": (f"read_csv_auto({_sql_literal(well_csv)}, header=true, all_varchar=true)"),
            "airports": (f"read_csv_auto({_sql_literal(faa_csv)}, header=true, all_varchar=true)"),
            "cities": (
                f"read_csv_auto({_sql_literal(census_tsv)}, delim='\\t', "
                "header=true, all_varchar=true, normalize_names=true)"
            ),
        }

        queries = {
            "earthquakes": f"""
                SELECT
                    CAST(id AS VARCHAR) AS event_id,
                    CAST(time AS TIMESTAMPTZ) AS event_time,
                    CAST(latitude AS DOUBLE) AS earthquake_latitude,
                    CAST(longitude AS DOUBLE) AS earthquake_longitude,
                    CAST(depth AS DOUBLE) AS depth_km,
                    CAST(mag AS DOUBLE) AS magnitude,
                    CAST(place AS VARCHAR) AS earthquake_place,
                    CAST(status AS VARCHAR) AS earthquake_review_status
                FROM {sources["earthquakes"]}
                WHERE latitude BETWEEN {_MIN_LATITUDE} AND {_MAX_LATITUDE}
                  AND longitude BETWEEN {_MIN_LONGITUDE} AND {_MAX_LONGITUDE}
                  AND id IS NOT NULL
                ORDER BY event_time, event_id
            """,
            "wells": f"""
                SELECT
                    CAST(api_well_number AS VARCHAR) AS api_well_number,
                    CAST(well_name AS VARCHAR) AS well_name,
                    CAST(town AS VARCHAR) AS well_town,
                    CAST(county AS VARCHAR) AS well_county,
                    CAST(well_type AS VARCHAR) AS well_type,
                    CAST(well_status AS VARCHAR) AS well_status,
                    try_cast(surface_latitude AS DOUBLE) AS well_latitude,
                    try_cast(surface_longitude AS DOUBLE) AS well_longitude,
                    try_cast(date_last_modified AS TIMESTAMP) AS last_modified
                FROM {sources["wells"]}
                WHERE try_cast(surface_latitude AS DOUBLE)
                        BETWEEN {_MIN_LATITUDE} AND {_MAX_LATITUDE}
                  AND try_cast(surface_longitude AS DOUBLE)
                        BETWEEN {_MIN_LONGITUDE} AND {_MAX_LONGITUDE}
                  AND api_well_number IS NOT NULL
                ORDER BY api_well_number
            """,
            "airports": f"""
                SELECT
                    CAST(SITE_NO AS VARCHAR) AS site_number,
                    CAST(ARPT_ID AS VARCHAR) AS airport_id,
                    CAST(ARPT_NAME AS VARCHAR) AS airport_name,
                    CAST(CITY AS VARCHAR) AS airport_city,
                    CAST(FACILITY_USE_CODE AS VARCHAR) AS airport_facility_use,
                    try_cast(LAT_DECIMAL AS DOUBLE) AS airport_latitude,
                    try_cast(LONG_DECIMAL AS DOUBLE) AS airport_longitude,
                    CAST(ARPT_STATUS AS VARCHAR) AS airport_status
                FROM {sources["airports"]}
                WHERE STATE_CODE = 'NY'
                  AND try_cast(LAT_DECIMAL AS DOUBLE)
                        BETWEEN {_MIN_LATITUDE} AND {_MAX_LATITUDE}
                  AND try_cast(LONG_DECIMAL AS DOUBLE)
                        BETWEEN {_MIN_LONGITUDE} AND {_MAX_LONGITUDE}
                  AND SITE_NO IS NOT NULL
                ORDER BY site_number
            """,
            "cities": f"""
                SELECT
                    CAST(geoid AS VARCHAR) AS geoid,
                    -- DuckDB prefixes the reserved header NAME during
                    -- normalize_names, so the stable normalized name is _name.
                    CAST(_name AS VARCHAR) AS place_name,
                    CAST(lsad AS VARCHAR) AS legal_statistical_area,
                    try_cast(intptlat AS DOUBLE) AS city_latitude,
                    try_cast(intptlong AS DOUBLE) AS city_longitude,
                    try_cast(aland_sqmi AS DOUBLE) AS land_area_sqmi
                FROM {sources["cities"]}
                WHERE usps = 'NY'
                  AND try_cast(intptlat AS DOUBLE)
                        BETWEEN {_MIN_LATITUDE} AND {_MAX_LATITUDE}
                  AND try_cast(intptlong AS DOUBLE)
                        BETWEEN {_MIN_LONGITUDE} AND {_MAX_LONGITUDE}
                  AND geoid IS NOT NULL
                ORDER BY geoid
            """,
        }

        derivations = {
            "earthquakes": (
                "USGS rows in the frozen NY bounding box; ordered by event time and event id."
            ),
            "wells": (
                "NYSDEC rows with valid surface coordinates in the frozen NY "
                "bounding box; ordered by API well number."
            ),
            "airports": (
                "FAA APT_BASE rows for New York with valid coordinates; ordered by site number."
            ),
            "cities": (
                "Census 2024 Places rows for New York with valid internal-point "
                "coordinates; ordered by GEOID."
            ),
        }

        for name in ("earthquakes", "wells", "airports", "cities"):
            notify(f"normalizing {name}")
            source_rows = _source_count(connection, sources[name])
            output = output_dir / f"{name}.parquet"
            _copy_query_to_parquet(connection, queries[name], output)
            outputs.append(
                _prepared_record(
                    connection,
                    artifact_id=f"multisource_{name}_v1",
                    output=output,
                    data_root=data_root,
                    source_rows=source_rows,
                    derivation=derivations[name],
                )
            )
    except Exception as exc:
        if isinstance(exc, MultisourcePreparationError):
            raise
        raise MultisourcePreparationError(f"Multisource preparation failed: {exc}") from exc
    finally:
        connection.close()
        if staging.exists():
            shutil.rmtree(staging)

    payload = {
        "schema_version": 1,
        "dataset_id": "multisource_case_study",
        "preparation_version": "v1",
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "geographic_scope": {
            "crs": "EPSG:4326",
            "minimum_latitude": _MIN_LATITUDE,
            "maximum_latitude": _MAX_LATITUDE,
            "minimum_longitude": _MIN_LONGITUDE,
            "maximum_longitude": _MAX_LONGITUDE,
        },
        "inputs": raw_inputs,
        "outputs": [asdict(item) for item in outputs],
        "claim_status": "PREPARED_NOT_YET_END_TO_END_VALIDATED",
    }
    manifest_path = data_root / "manifests" / "processed" / "multisource-case-v1.json"
    _atomic_json(manifest_path, payload)
    return payload
