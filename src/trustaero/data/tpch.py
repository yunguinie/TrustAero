"""Deterministic E-drive preparation for frozen TPC-H scale factors.

SF1 remains the inexpensive development reference.  SF10 is the first
paper-scale database artifact: it contains roughly sixty million ``lineitem``
rows and is generated locally from DuckDB's signed TPC-H extension.  Keeping
generation here (instead of downloading an opaque database) lets us record the
exact extension version, row counts, file digest, and source commit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file

TPCH_SF1_EXPECTED_ROWS = {
    "customer": 150_000,
    "lineitem": 6_001_215,
    "nation": 25,
    "orders": 1_500_000,
    "part": 200_000,
    "partsupp": 800_000,
    "region": 5,
    "supplier": 10_000,
}

# Exact row counts produced by the standard deterministic dbgen population.
# Only scale factors that have a reviewed, explicit contract are accepted;
# silently extrapolating ``lineitem`` would be wrong because its count is not a
# fixed integer multiple of SF1.
TPCH_SF10_EXPECTED_ROWS = {
    "customer": 1_500_000,
    "lineitem": 59_986_052,
    "nation": 25,
    "orders": 15_000_000,
    "part": 2_000_000,
    "partsupp": 8_000_000,
    "region": 5,
    "supplier": 100_000,
}

TPCH_EXPECTED_ROWS_BY_SCALE = {
    1: TPCH_SF1_EXPECTED_ROWS,
    10: TPCH_SF10_EXPECTED_ROWS,
}


class TpchPreparationError(RuntimeError):
    """Raised when TPC-H preparation cannot satisfy its frozen contract."""


@dataclass(frozen=True, slots=True)
class TpchPreparedArtifact:
    database_path: str
    scale_factor: int
    byte_size: int
    sha256: str
    table_rows: dict[str, int]
    query_count: int
    source_commit: str
    duckdb_version: str
    tpch_extension_version: str


def _sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _resume_partition(
    building_database: Path,
    checkpoint_path: Path,
    *,
    scale_factor: int,
    source_commit: str,
) -> int:
    """Return the first unfinished dbgen partition, or safely restart at zero.

    The sidecar advances only after DuckDB checkpoints a complete partition.
    A stale or malformed sidecar is discarded with its unpublished database,
    so partial data can never be mistaken for a formal artifact.
    """

    if not building_database.exists() and not checkpoint_path.exists():
        return 0
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        completed = int(payload["completed_partitions"])
        valid = (
            building_database.is_file()
            and payload.get("scale_factor") == scale_factor
            and payload.get("source_commit") == source_commit
            and 0 <= completed <= 8
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        valid = False
        completed = 0
    if valid:
        return completed
    building_database.unlink(missing_ok=True)
    checkpoint_path.unlink(missing_ok=True)
    return 0


def _git_state(root: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise TpchPreparationError("TPC-H generation requires a Git repository") from exc
    return commit, dirty


def _inside(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise TpchPreparationError(f"TPC-H path escapes the project root: {path}")
    return resolved


def _extension_version(connection: Any) -> str:
    row = connection.execute(
        "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'tpch'"
    ).fetchone()
    if row is None or row[0] is None:
        raise TpchPreparationError("loaded TPC-H extension has no version")
    return str(row[0])


def _validate_database(connection: Any, *, scale_factor: int) -> tuple[dict[str, int], int]:
    expected_rows = TPCH_EXPECTED_ROWS_BY_SCALE.get(scale_factor)
    if expected_rows is None:
        raise TpchPreparationError(f"TPC-H SF{scale_factor} has no reviewed exact-row contract")
    rows = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in expected_rows
    }
    if rows != expected_rows:
        raise TpchPreparationError(f"TPC-H SF{scale_factor} row counts differ: {rows}")
    query_count = int(connection.execute("SELECT COUNT(*) FROM tpch_queries()").fetchone()[0])
    if query_count != 22:
        raise TpchPreparationError(f"TPC-H query catalog contains {query_count}, expected 22")
    return rows, query_count


def prepare_tpch_scale(
    project_root: Path,
    *,
    scale_factor: int,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> TpchPreparedArtifact:
    """Install the signed extension and atomically generate SF1 or SF10.

    A completed artifact is reused only after its size, SHA-256 digest, scale
    factor, and all eight table counts match the manifest.  An interrupted
    build remains under a ``.building`` name and is never mistaken for input to
    a formal experiment.
    """

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise TpchPreparationError("DuckDB is required for TPC-H preparation") from exc
    expected_rows = TPCH_EXPECTED_ROWS_BY_SCALE.get(scale_factor)
    if expected_rows is None:
        supported = ", ".join(str(value) for value in sorted(TPCH_EXPECTED_ROWS_BY_SCALE))
        raise TpchPreparationError(
            f"Unsupported TPC-H scale factor {scale_factor}; reviewed values: {supported}"
        )
    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if dirty:
        raise TpchPreparationError("TPC-H generation requires a clean committed source tree")
    scale_id = f"sf{scale_factor}"
    database = _inside(root, root / f"data/processed/tpch/{scale_id}/tpch_{scale_id}.duckdb")
    manifest = _inside(root, root / f"data/manifests/processed/tpch-{scale_id}.json")
    extension_directory = _inside(root, root / "data/processed/duckdb_extensions")
    database.parent.mkdir(parents=True, exist_ok=True)
    extension_directory.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(database.parent).free
    # The threshold includes the published DB, the atomic build file, DuckDB
    # temporary space, and enough headroom to avoid filling the experiment disk.
    required_free_bytes = (2 if scale_factor == 1 else 12) * 1024**3
    if free_bytes < required_free_bytes:
        required_gib = required_free_bytes // 1024**3
        raise TpchPreparationError(
            f"TPC-H SF{scale_factor} preparation requires at least {required_gib} GiB free space"
        )

    if database.is_file() and manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            int(payload.get("byte_size", -1)) == database.stat().st_size
            and str(payload.get("sha256", "")) == sha256_file(database)
            and int(payload.get("scale_factor", -1)) == scale_factor
            and payload.get("table_rows") == expected_rows
        ):
            return TpchPreparedArtifact(
                database_path=str(database.relative_to(root)).replace("\\", "/"),
                scale_factor=scale_factor,
                byte_size=database.stat().st_size,
                sha256=str(payload["sha256"]),
                table_rows=dict(expected_rows),
                query_count=int(payload["query_count"]),
                source_commit=str(payload["source_commit"]),
                duckdb_version=str(payload["duckdb_version"]),
                tpch_extension_version=str(payload["tpch_extension_version"]),
            )

    temporary = database.with_suffix(".duckdb.building")
    checkpoint = database.with_suffix(".duckdb.building.state.json")
    start_partition = _resume_partition(
        temporary,
        checkpoint,
        scale_factor=scale_factor,
        source_commit=commit,
    )
    started = time.monotonic()
    extension_literal = _sql_literal(extension_directory)
    setup = duckdb.connect()
    try:
        setup.execute(f"SET extension_directory = {extension_literal}")
        if progress:
            progress(0, 10, "install signed DuckDB tpch extension on E drive", 0.0)
        setup.execute("INSTALL tpch FROM core")
    finally:
        setup.close()

    connection = duckdb.connect(str(temporary))
    try:
        connection.execute(f"SET extension_directory = {extension_literal}")
        connection.execute("LOAD tpch")
        if progress and start_partition:
            progress(
                start_partition,
                10,
                f"resume SF{scale_factor} after {start_partition}/8 partitions",
                time.monotonic() - started,
            )
        for step in range(start_partition, 8):
            if progress:
                progress(
                    step + 1,
                    10,
                    f"generate SF{scale_factor} partition {step + 1}/8",
                    time.monotonic() - started,
                )
            connection.execute(f"CALL dbgen(sf = {scale_factor}, children = 8, step = {step})")
            # Publish a resumable boundary only after DuckDB persists the
            # partition. An interrupted call is repeated safely on the rerun.
            connection.execute("CHECKPOINT")
            _atomic_json(
                checkpoint,
                {
                    "schema_version": 1,
                    "scale_factor": scale_factor,
                    "source_commit": commit,
                    "completed_partitions": step + 1,
                },
            )
        if progress:
            progress(9, 10, "verify eight tables and 22 queries", time.monotonic() - started)
        table_rows, query_count = _validate_database(connection, scale_factor=scale_factor)
        extension_version = _extension_version(connection)
        version_row = connection.execute("SELECT version()").fetchone()
        if version_row is None:
            raise TpchPreparationError("DuckDB version query returned no row")
        duckdb_version = str(version_row[0])
        connection.execute("CHECKPOINT")
    except BaseException:
        connection.close()
        # Preserve only the unpublished build and last completed checkpoint;
        # the next invocation resumes instead of repeating finished work.
        raise
    else:
        connection.close()
    os.replace(temporary, database)
    checkpoint.unlink(missing_ok=True)
    if progress:
        progress(
            10,
            10,
            f"hash and publish SF{scale_factor}",
            time.monotonic() - started,
        )
    artifact = TpchPreparedArtifact(
        database_path=str(database.relative_to(root)).replace("\\", "/"),
        scale_factor=scale_factor,
        byte_size=database.stat().st_size,
        sha256=sha256_file(database),
        table_rows=table_rows,
        query_count=query_count,
        source_commit=commit,
        duckdb_version=duckdb_version,
        tpch_extension_version=extension_version,
    )
    _atomic_json(
        manifest,
        {
            "schema_version": 1,
            "artifact_id": f"tpch_{scale_id}_duckdb_v1",
            **asdict(artifact),
            "generation": "DuckDB signed core tpch extension; dbgen children=8, steps=0..7",
            "scientific_boundary": (
                "TPC-H is a standard relational benchmark. Governance policies and "
                "sensitive attributes are separate deterministic TrustAero augmentations."
            ),
        },
    )
    return artifact


def prepare_tpch_sf1(
    project_root: Path,
    *,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> TpchPreparedArtifact:
    """Backward-compatible SF1 entry point used by the frozen development run."""

    return prepare_tpch_scale(project_root, scale_factor=1, progress=progress)
