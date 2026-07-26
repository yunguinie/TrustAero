"""Tests for resumable full-year real-data preparation."""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import duckdb
import pytest

import trustaero.data.prepare_year as year_module
from trustaero.data.download import sha256_file
from trustaero.data.prepare import _BTS_COLUMNS, PreparationError
from trustaero.data.prepare_year import normalize_2024_months, prepare_real_data_2024


def _write_audit(project_root: Path, artifact_id: str, path: Path) -> None:
    audit = project_root / "data/manifests/downloads" / f"{artifact_id}.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps(
            {
                "local_path": path.relative_to(project_root).as_posix(),
                "byte_size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        ),
        encoding="utf-8",
    )


def _create_month_fixture(project_root: Path, month: int = 2) -> None:
    data = project_root / "data"
    bts = data / (
        "raw/bts/on_time/2024/"
        f"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_2024_{month}.zip"
    )
    bts.parent.mkdir(parents=True, exist_ok=True)
    csv_path = project_root / "fixture.csv"
    values = {name: "1" for name in _BTS_COLUMNS}
    values.update(
        {
            "FlightDate": f"2024-{month:02d}-01",
            "Reporting_Airline": "AA",
            "Tail_Number": "N123AA",
            "Origin": "AAA",
            "OriginCityName": "Alpha City",
            "OriginState": "AA",
            "Dest": "BBB",
            "DestCityName": "Beta City",
            "DestState": "BB",
        }
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_BTS_COLUMNS))
        writer.writeheader()
        writer.writerow(values)
    with zipfile.ZipFile(bts, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, arcname="monthly.csv")
    csv_path.unlink()

    nyc = data / f"raw/nyc_tlc/yellow/2024/yellow_tripdata_2024-{month:02d}.parquet"
    nyc.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute(
            "COPY (SELECT TIMESTAMP '2024-02-01 12:00:00' AS "
            "tpep_pickup_datetime, 1::BIGINT AS PULocationID) "
            f"TO '{nyc.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        connection.close()
    _write_audit(project_root, f"bts_on_time_2024_{month:02d}", bts)
    _write_audit(project_root, f"nyc_tlc_yellow_2024_{month:02d}", nyc)


def test_normalize_2024_months_is_sorted_and_fail_closed() -> None:
    assert normalize_2024_months((12, 2, 2, 7)) == (2, 7, 12)
    with pytest.raises(PreparationError, match="1 through 12"):
        normalize_2024_months((0, 2))


def test_prepare_month_writes_audited_outputs_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_month_fixture(tmp_path)
    first = prepare_real_data_2024(tmp_path / "data", months=(2,))
    assert first["status"] == "PASS"
    assert first["month_count"] == 1
    assert first["bts_total_rows"] == 1
    assert first["nyc_total_rows"] == 1

    month_manifest = tmp_path / "data/manifests/processed/real-data-2024-02.json"
    payload = json.loads(month_manifest.read_text(encoding="utf-8"))
    assert len(payload["outputs"]) == 3
    assert all((tmp_path / "data" / item["relative_path"]).is_file() for item in payload["outputs"])

    # A valid month manifest must prevent expensive conversion from running
    # again; resume still re-hashes every bound input and output first.
    monkeypatch.setattr(
        year_module,
        "_prepare_bts_month",
        lambda *_args, **_kwargs: pytest.fail("completed month was rebuilt"),
    )
    resumed = prepare_real_data_2024(tmp_path / "data", months=(2,))
    assert resumed["bts_total_rows"] == 1
