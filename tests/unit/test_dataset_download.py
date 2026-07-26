"""Unit tests for fail-closed, resumable dataset acquisition."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

import trustaero.data.download as download_module
from trustaero.data.download import (
    ArtifactSpec,
    DownloadError,
    download_artifact,
    load_artifact_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeResponse(io.BytesIO):
    """Small context-managed HTTP response used without network access."""

    def __init__(self, body: bytes, *, status: int, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _spec(*, expected_bytes: int = 6, expected_sha256: str | None = None) -> ArtifactSpec:
    return ArtifactSpec(
        artifact_id="fixture",
        dataset_id="fixture_dataset",
        url="https://example.test/fixture.bin",
        relative_path="raw/fixture/fixture.bin",
        stage="test",
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
    )


def test_registry_rejects_duplicate_artifact_ids(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    entry: dict[str, Any] = {
        "artifact_id": "duplicate",
        "dataset_id": "dataset",
        "url": "https://example.test/a",
        "relative_path": "raw/a",
        "stage": "test",
    }
    registry.write_text(json.dumps({"artifacts": [entry, entry]}), encoding="utf-8")

    with pytest.raises(DownloadError, match="duplicate artifact_id"):
        load_artifact_registry(registry)


def test_frozen_2024_registry_covers_every_bts_and_nyc_month() -> None:
    """Keep the final real-data acquisition command complete and deterministic."""

    artifacts = load_artifact_registry(PROJECT_ROOT / "data/manifests/dataset-registry.json")
    by_dataset = {
        dataset_id: {
            artifact.artifact_id for artifact in artifacts if artifact.dataset_id == dataset_id
        }
        for dataset_id in ("bts_on_time_2024", "nyc_tlc_yellow_2024")
    }
    expected_bts = {f"bts_on_time_2024_{month:02d}" for month in range(1, 13)}
    expected_nyc = {f"nyc_tlc_yellow_2024_{month:02d}" for month in range(1, 13)}
    # The NYC dataset also contains one shared zone lookup, so only compare its
    # month-prefixed artifacts here.
    assert by_dataset["bts_on_time_2024"] == expected_bts
    assert {
        artifact_id
        for artifact_id in by_dataset["nyc_tlc_yellow_2024"]
        if artifact_id.startswith("nyc_tlc_yellow_2024_")
    } == expected_nyc

    main = [artifact for artifact in artifacts if artifact.stage == "main_2024"]
    assert len(main) == 22
    assert all(artifact.artifact_id[-2:] != "01" for artifact in main)


def test_multisource_case_registry_covers_four_frozen_publishers() -> None:
    """Prevent the end-to-end case from silently losing a source or its audit path."""

    artifacts = load_artifact_registry(PROJECT_ROOT / "data/manifests/dataset-registry.json")
    case_artifacts = {
        artifact.artifact_id: artifact
        for artifact in artifacts
        if artifact.stage == "multisource_case_v1"
    }

    assert set(case_artifacts) == {
        "multisource_usgs_earthquakes_ny_2000_2024",
        "multisource_nysdec_regulated_wells_20260724",
        "multisource_faa_airports_20241128",
        "multisource_census_places_2024",
    }
    assert all(
        artifact.dataset_id == "multisource_case_study" for artifact in case_artifacts.values()
    )
    assert all(artifact.url.startswith("https://") for artifact in case_artifacts.values())
    assert all(
        artifact.relative_path.startswith("raw/multisource/")
        for artifact in case_artifacts.values()
    )


def test_download_rejects_destination_outside_data_root(tmp_path: Path) -> None:
    unsafe = ArtifactSpec(
        artifact_id="unsafe",
        dataset_id="dataset",
        url="https://example.test/unsafe",
        relative_path="../outside.bin",
        stage="test",
    )

    with pytest.raises(DownloadError, match="escapes the data root"):
        download_artifact(unsafe, tmp_path / "data")


def test_fresh_download_is_verified_and_atomically_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"abcdef"
    expected_hash = hashlib.sha256(body).hexdigest()

    def fake_urlopen(_request: object, *, timeout: float) -> FakeResponse:
        assert timeout == 60.0
        return FakeResponse(body, status=200, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(download_module, "urlopen", fake_urlopen)
    progress: list[tuple[int, int | None]] = []
    result = download_artifact(
        _spec(expected_sha256=expected_hash),
        tmp_path / "data",
        progress=lambda current, total: progress.append((current, total)),
    )

    assert result.path.read_bytes() == body
    assert result.sha256 == expected_hash
    assert result.reused_existing_file is False
    assert not result.path.with_name("fixture.bin.part").exists()
    assert progress[-1] == (len(body), len(body))


def test_partial_download_resumes_when_server_honours_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    part_path = data_root / "raw" / "fixture" / "fixture.bin.part"
    part_path.parent.mkdir(parents=True)
    part_path.write_bytes(b"abc")

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        assert timeout == 60.0
        assert request.get_header("Range") == "bytes=3-"
        return FakeResponse(
            b"def",
            status=206,
            headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3"},
        )

    monkeypatch.setattr(download_module, "urlopen", fake_urlopen)
    result = download_artifact(_spec(), data_root)

    assert result.path.read_bytes() == b"abcdef"


def test_server_ignoring_range_restarts_instead_of_duplicating_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    part_path = data_root / "raw" / "fixture" / "fixture.bin.part"
    part_path.parent.mkdir(parents=True)
    part_path.write_bytes(b"abc")

    monkeypatch.setattr(
        download_module,
        "urlopen",
        lambda _request, *, timeout: FakeResponse(
            b"abcdef", status=200, headers={"Content-Length": "6"}
        ),
    )
    result = download_artifact(_spec(), data_root)

    assert result.path.read_bytes() == b"abcdef"


def test_checksum_failure_never_publishes_final_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        download_module,
        "urlopen",
        lambda _request, *, timeout: FakeResponse(
            b"abcdef", status=200, headers={"Content-Length": "6"}
        ),
    )
    wrong_hash = "0" * 64
    destination = tmp_path / "data" / "raw" / "fixture" / "fixture.bin"

    with pytest.raises(DownloadError, match="SHA-256 mismatch"):
        download_artifact(_spec(expected_sha256=wrong_hash), tmp_path / "data")

    assert not destination.exists()
    assert destination.with_name("fixture.bin.part").exists()
