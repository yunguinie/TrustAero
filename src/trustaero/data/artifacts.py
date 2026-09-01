"""Verify prepared real-data artifacts before governed execution."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file


class PreparedArtifactVerificationError(RuntimeError):
    """Raised when a derived execution artifact differs from its manifest."""


@dataclass(frozen=True, slots=True)
class VerifiedPreparedArtifact:
    """Immutable execution binding for one checked derived data file."""

    artifact_id: str
    relative_path: str
    row_count: int
    byte_size: int
    sha256: str


def _manifest_outputs(data_root: Path) -> dict[str, dict[str, Any]]:
    path = data_root / "manifests/processed/real-data-smoke.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        outputs = payload["outputs"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PreparedArtifactVerificationError(
            f"Prepared artifact manifest is missing or malformed: {path}"
        ) from exc
    if not isinstance(outputs, list):
        raise PreparedArtifactVerificationError("Prepared artifact outputs must be a list")
    result: dict[str, dict[str, Any]] = {}
    for item in outputs:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
            raise PreparedArtifactVerificationError("Prepared artifact entry is malformed")
        result[str(item["relative_path"]).replace("\\", "/")] = item
    return result


def _manifest_inputs(data_root: Path) -> dict[str, dict[str, Any]]:
    path = data_root / "manifests/processed/real-data-smoke.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        inputs = payload["inputs"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PreparedArtifactVerificationError(
            f"Prepared artifact manifest inputs are missing or malformed: {path}"
        ) from exc
    if not isinstance(inputs, list):
        raise PreparedArtifactVerificationError("Prepared artifact inputs must be a list")
    return {
        str(item["relative_path"]).replace("\\", "/"): item
        for item in inputs
        if isinstance(item, dict) and isinstance(item.get("relative_path"), str)
    }


def verify_real_data_slice_artifacts(
    data_root: Path,
    sample_rows: int,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Verify BTS, NYC, and zone files used by one real-data slice."""

    if sample_rows < 1:
        raise ValueError("sample_rows must be positive")
    expected = (
        f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet",
        f"processed/nyc_tlc/yellow/2024-01/yellow_taxi_{sample_rows}.parquet",
        "processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet",
    )
    outputs = _manifest_outputs(data_root)
    try:
        duckdb = importlib.import_module("duckdb")
    except ModuleNotFoundError as exc:  # pragma: no cover - runners require DuckDB
        raise PreparedArtifactVerificationError(
            "DuckDB is required to verify prepared Parquet row counts"
        ) from exc
    connection = duckdb.connect()
    verified: list[VerifiedPreparedArtifact] = []
    try:
        for relative_path in expected:
            entry = outputs.get(relative_path)
            if entry is None:
                raise PreparedArtifactVerificationError(
                    f"Prepared artifact is absent from manifest: {relative_path}"
                )
            path = data_root / Path(relative_path)
            if not path.is_file():
                raise PreparedArtifactVerificationError(f"Prepared artifact is missing: {path}")
            byte_size = path.stat().st_size
            expected_size = int(entry.get("byte_size", -1))
            expected_digest = str(entry.get("sha256", ""))
            row_count = int(entry.get("row_count", -1))
            if byte_size != expected_size:
                raise PreparedArtifactVerificationError(
                    f"Prepared artifact byte size changed: {relative_path}"
                )
            actual_digest = sha256_file(path)
            if actual_digest != expected_digest:
                raise PreparedArtifactVerificationError(
                    f"Prepared artifact SHA-256 changed: {relative_path}"
                )
            escaped = str(path).replace("'", "''")
            actual_rows = int(
                connection.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            )
            if row_count < 1 or actual_rows != row_count:
                raise PreparedArtifactVerificationError(
                    f"Prepared artifact row count changed: {relative_path}"
                )
            verified.append(
                VerifiedPreparedArtifact(
                    artifact_id=str(entry.get("artifact_id", "")),
                    relative_path=relative_path,
                    row_count=row_count,
                    byte_size=byte_size,
                    sha256=actual_digest,
                )
            )
    finally:
        connection.close()
    return tuple(verified)


def _verify_processed_outputs(
    data_root: Path,
    expected: tuple[str, ...],
    *,
    label: str,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Verify an exact list of processed files against the frozen manifest."""

    outputs = _manifest_outputs(data_root)
    try:
        duckdb = importlib.import_module("duckdb")
    except ModuleNotFoundError as exc:  # pragma: no cover - runners require DuckDB
        raise PreparedArtifactVerificationError(
            "DuckDB is required to verify prepared Parquet row counts"
        ) from exc
    connection = duckdb.connect()
    verified: list[VerifiedPreparedArtifact] = []
    try:
        for relative_path in expected:
            entry = outputs.get(relative_path)
            if entry is None:
                raise PreparedArtifactVerificationError(
                    f"{label} artifact is absent from manifest: {relative_path}"
                )
            path = data_root / relative_path
            if not path.is_file() or path.stat().st_size != int(entry.get("byte_size", -1)):
                raise PreparedArtifactVerificationError(
                    f"{label} artifact size changed: {relative_path}"
                )
            digest = sha256_file(path)
            if digest != str(entry.get("sha256", "")):
                raise PreparedArtifactVerificationError(
                    f"{label} artifact SHA-256 changed: {relative_path}"
                )
            escaped = str(path).replace("'", "''")
            row_count = int(
                connection.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            )
            if row_count != int(entry.get("row_count", -1)):
                raise PreparedArtifactVerificationError(
                    f"{label} artifact row count changed: {relative_path}"
                )
            verified.append(
                VerifiedPreparedArtifact(
                    artifact_id=str(entry.get("artifact_id", "")),
                    relative_path=relative_path,
                    row_count=row_count,
                    byte_size=path.stat().st_size,
                    sha256=digest,
                )
            )
    finally:
        connection.close()
    return tuple(verified)


def verify_bts_multijoin_slice_artifacts(
    data_root: Path,
    sample_rows: int,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Verify the fact and two dimensions used by the BTS natural Join smoke.

    This is separate from ``verify_real_data_slice_artifacts`` so the earlier
    BTS/NYC integration contract does not silently acquire new inputs.
    """

    if sample_rows < 1:
        raise ValueError("sample_rows must be positive")
    return _verify_processed_outputs(
        data_root,
        (
            f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet",
            "processed/bts/on_time/2024-01/bts_airports.parquet",
            "processed/bts/on_time/2024-01/bts_carriers.parquet",
        ),
        label="BTS multi-Join",
    )


def verify_bts_multijoin_full_month_artifacts(
    data_root: Path,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Verify the full January fact and both natural BTS dimensions."""

    return _verify_processed_outputs(
        data_root,
        (
            "processed/bts/on_time/2024-01/bts_flights_full.parquet",
            "processed/bts/on_time/2024-01/bts_airports.parquet",
            "processed/bts/on_time/2024-01/bts_carriers.parquet",
        ),
        label="BTS full-month multi-Join",
    )


def verify_bts_mask_join_slice_artifacts(
    data_root: Path,
    sample_rows: int,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Verify only the fact and airport files used by the Mask/Join smoke."""

    if sample_rows < 1:
        raise ValueError("sample_rows must be positive")
    return _verify_processed_outputs(
        data_root,
        (
            f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet",
            "processed/bts/on_time/2024-01/bts_airports.parquet",
        ),
        label="BTS Mask/Join",
    )


def verify_bts_mask_join_full_month_artifacts(
    data_root: Path,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Verify the immutable full January fact and airport dimension files."""

    return _verify_processed_outputs(
        data_root,
        (
            "processed/bts/on_time/2024-01/bts_flights_full.parquet",
            "processed/bts/on_time/2024-01/bts_airports.parquet",
        ),
        label="BTS full-month Mask/Join",
    )


def verify_real_data_full_month_artifacts(
    data_root: Path,
    workload: str,
) -> tuple[VerifiedPreparedArtifact, ...]:
    """Bind the exact full-month execution files for one workload."""

    definitions = {
        "bts": (
            (
                "processed/bts/on_time/2024-01/bts_flights_full.parquet",
                547_271,
                "output",
            ),
        ),
        "nyc_tlc": (
            (
                "raw/nyc_tlc/yellow/2024/yellow_tripdata_2024-01.parquet",
                2_964_624,
                "input",
            ),
            ("processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet", 265, "output"),
        ),
    }
    if workload not in definitions:
        raise ValueError(f"unsupported full-month workload: {workload}")
    outputs = _manifest_outputs(data_root)
    inputs = _manifest_inputs(data_root)
    try:
        duckdb = importlib.import_module("duckdb")
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise PreparedArtifactVerificationError("DuckDB is required for row verification") from exc
    connection = duckdb.connect()
    verified: list[VerifiedPreparedArtifact] = []
    try:
        for relative_path, expected_rows, source in definitions[workload]:
            entry = (outputs if source == "output" else inputs).get(relative_path)
            if entry is None:
                raise PreparedArtifactVerificationError(
                    f"Full-month artifact is absent from manifest: {relative_path}"
                )
            path = data_root / relative_path
            if not path.is_file() or path.stat().st_size != int(entry["byte_size"]):
                raise PreparedArtifactVerificationError(
                    f"Full-month artifact size changed: {relative_path}"
                )
            digest = sha256_file(path)
            if digest != str(entry["sha256"]):
                raise PreparedArtifactVerificationError(
                    f"Full-month artifact SHA-256 changed: {relative_path}"
                )
            escaped = str(path).replace("'", "''")
            actual_rows = int(
                connection.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            )
            if actual_rows != expected_rows:
                raise PreparedArtifactVerificationError(
                    f"Full-month artifact row count changed: {relative_path}"
                )
            verified.append(
                VerifiedPreparedArtifact(
                    artifact_id=str(entry["artifact_id"]),
                    relative_path=relative_path,
                    row_count=actual_rows,
                    byte_size=path.stat().st_size,
                    sha256=digest,
                )
            )
    finally:
        connection.close()
    return tuple(verified)
