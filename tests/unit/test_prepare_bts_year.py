"""Tests for generic, resumable BTS calendar-year preparation."""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest

import trustaero.data.prepare_bts_year as module
from trustaero.data.download import sha256_file
from trustaero.data.prepare import _BTS_COLUMNS, PreparationError
from trustaero.data.prepare_bts_year import normalize_calendar_months, prepare_bts_calendar_year


def _fixture(root: Path, year: int, month: int) -> None:
    name = f"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
    target = root / f"data/raw/bts/on_time/{year}/{name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    csv_path = root / "fixture.csv"
    values = {name: "1" for name in _BTS_COLUMNS}
    values.update(
        {
            "FlightDate": f"{year}-{month:02d}-01",
            "Reporting_Airline": "AA",
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
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, arcname="monthly.csv")
    csv_path.unlink()
    audit = root / f"data/manifests/downloads/bts_on_time_{year}_{month:02d}.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps(
            {
                "artifact": {"url": "https://example.invalid/frozen.zip"},
                "local_path": target.relative_to(root).as_posix(),
                "byte_size": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        ),
        encoding="utf-8",
    )


def test_month_normalization_is_fail_closed() -> None:
    assert normalize_calendar_months((12, 1, 1, 7)) == (1, 7, 12)
    with pytest.raises(PreparationError, match="1 through 12"):
        normalize_calendar_months((13,))


def test_prepare_bts_year_writes_verified_outputs_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture(tmp_path, 2025, 1)
    first = prepare_bts_calendar_year(tmp_path / "data", year=2025, months=(1,))
    assert first["status"] == "PASS"
    assert first["month_count"] == 1
    assert first["bts_total_rows"] == 1
    manifest = tmp_path / "data/manifests/processed/bts-2025-01.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(payload["outputs"]) == 3
    monkeypatch.setattr(
        module,
        "_prepare_month",
        lambda *_args, **_kwargs: pytest.fail("verified month was rebuilt"),
    )
    resumed = prepare_bts_calendar_year(tmp_path / "data", year=2025, months=(1,))
    assert resumed["bts_total_rows"] == 1
