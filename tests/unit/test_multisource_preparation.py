"""Tests for fail-closed, deterministic four-source preparation."""

from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from trustaero.data.multisource import (
    MultisourcePreparationError,
    prepare_multisource_case,
)


def _write_audit(data_root: Path, artifact_id: str, relative_path: str) -> None:
    raw = data_root / relative_path
    audit = {
        "schema_version": 1,
        "artifact": {"artifact_id": artifact_id},
        "local_path": f"data/{relative_path}",
        "byte_size": raw.stat().st_size,
        "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
    }
    path = data_root / "manifests" / "downloads" / f"{artifact_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit), encoding="utf-8")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_zip_member(path: Path, member: str, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)


def _fixture_data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    earthquake = data_root / "raw/multisource/usgs/earthquakes_ny_2000_2024.csv"
    _write_csv(
        earthquake,
        ["time", "latitude", "longitude", "depth", "mag", "id", "place", "status"],
        [
            {
                "time": "2024-01-01T00:00:00Z",
                "latitude": "42.0",
                "longitude": "-75.0",
                "depth": "5",
                "mag": "2.1",
                "id": "us-a",
                "place": "New York",
                "status": "reviewed",
            },
            {
                "time": "2024-01-02T00:00:00Z",
                "latitude": "35.0",
                "longitude": "-75.0",
                "depth": "4",
                "mag": "1.0",
                "id": "outside",
                "place": "Outside",
                "status": "automatic",
            },
        ],
    )

    wells = data_root / "raw/multisource/nysdec/regulated_wells_20260724.csv"
    _write_csv(
        wells,
        [
            "api_well_number",
            "well_name",
            "town",
            "county",
            "well_type",
            "well_status",
            "surface_latitude",
            "surface_longitude",
            "date_last_modified",
        ],
        [
            {
                "api_well_number": "3100100001",
                "well_name": "Fixture Well",
                "town": "Albany",
                "county": "Albany",
                "well_type": "Gas",
                "well_status": "Active",
                "surface_latitude": "42.6",
                "surface_longitude": "-73.8",
                "date_last_modified": "2024-01-01",
            },
            {
                "api_well_number": "3100100002",
                "well_name": "Missing Coordinates",
                "town": "Albany",
                "county": "Albany",
                "well_type": "Gas",
                "well_status": "Inactive",
                "surface_latitude": "",
                "surface_longitude": "",
                "date_last_modified": "2024-01-01",
            },
        ],
    )

    faa = data_root / "raw/multisource/faa/28_Nov_2024_APT_CSV.zip"
    faa_rows = [
        [
            "SITE_NO",
            "STATE_CODE",
            "ARPT_ID",
            "ARPT_NAME",
            "CITY",
            "FACILITY_USE_CODE",
            "LAT_DECIMAL",
            "LONG_DECIMAL",
            "ARPT_STATUS",
        ],
        ["0001", "NY", "ALB", "Albany Intl", "Albany", "PU", "42.7", "-73.8", "O"],
        ["0002", "NJ", "OUT", "Outside", "Outside", "PU", "40.8", "-74.0", "O"],
    ]
    faa_content = "\n".join(",".join(row) for row in faa_rows) + "\n"
    _write_zip_member(faa, "APT_BASE.csv", faa_content)

    census = data_root / "raw/multisource/census/2024_Gaz_place_national.zip"
    census_content = (
        "USPS\tGEOID\tNAME\tLSAD\tALAND_SQMI\tINTPTLAT\tINTPTLONG\n"
        "NY\t3601000\tAlbany\t25\t21.4\t42.65\t-73.75\n"
        "NJ\t3401000\tOutside\t25\t1.0\t40.80\t-74.00\n"
    )
    _write_zip_member(census, "2024_Gaz_place_national.txt", census_content)

    artifacts = {
        "multisource_usgs_earthquakes_ny_2000_2024": earthquake,
        "multisource_nysdec_regulated_wells_20260724": wells,
        "multisource_faa_airports_20241128": faa,
        "multisource_census_places_2024": census,
    }
    for artifact_id, path in artifacts.items():
        _write_audit(data_root, artifact_id, path.relative_to(data_root).as_posix())
    return data_root


def test_prepare_multisource_case_normalizes_and_filters_sources(
    tmp_path: Path,
) -> None:
    data_root = _fixture_data_root(tmp_path)

    result = prepare_multisource_case(data_root)

    assert [item["row_count"] for item in result["outputs"]] == [1, 1, 1, 1]
    assert [item["source_rows"] for item in result["outputs"]] == [2, 2, 2, 2]
    assert all(item["dropped_rows"] == 1 for item in result["outputs"])
    assert result["claim_status"] == "PREPARED_NOT_YET_END_TO_END_VALIDATED"
    assert (data_root / "manifests/processed/multisource-case-v1.json").is_file()


def test_prepare_multisource_case_rejects_raw_file_changed_after_audit(
    tmp_path: Path,
) -> None:
    data_root = _fixture_data_root(tmp_path)
    raw = data_root / "raw/multisource/usgs/earthquakes_ny_2000_2024.csv"
    raw.write_text(raw.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(MultisourcePreparationError, match="no longer matches"):
        prepare_multisource_case(data_root)
