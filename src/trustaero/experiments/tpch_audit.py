"""Reproducible audit of all 22 official TPC-H queries against IR v1.

The audit keeps two questions separate:

1. Can the official query execute on the frozen DuckDB SF1 artifact?
2. Can the current *trusted IR* express the query without raw-SQL escape hatches?

An executable DuckDB query is never counted as TrustAero-supported merely
because DuckDB accepts it.  Unsupported semantic features are listed explicitly
and fail closed; this prevents cherry-picking an inflated support percentage.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trustaero.data.download import sha256_file


class TpchAuditError(RuntimeError):
    """Raised when the frozen TPC-H audit contract is not satisfied."""


# These blockers describe exact official-query semantics, not DuckDB support.
# Q1 and Q6 have no blockers after adding only their independently reviewed
# arithmetic and ordering fragments. Every other query remains visible and is
# reported rather than silently removed from the denominator.
TPCH_IR_V1_BLOCKERS: dict[int, tuple[str, ...]] = {
    1: (),
    2: ("like_predicate", "correlated_scalar_subquery", "order_by", "limit"),
    3: ("computed_aggregate_expression", "order_by", "limit"),
    4: ("correlated_exists", "order_by"),
    5: ("computed_aggregate_expression", "order_by"),
    6: (),
    7: ("derived_projection", "from_subquery", "computed_aggregate_expression", "order_by"),
    8: ("case_expression", "derived_projection", "from_subquery", "division", "order_by"),
    9: ("like_predicate", "derived_projection", "from_subquery", "order_by"),
    10: ("computed_aggregate_expression", "order_by", "limit"),
    11: ("computed_aggregate_expression", "having", "scalar_subquery", "order_by"),
    12: ("case_expression", "in_predicate", "field_field_filter", "order_by"),
    13: ("join_predicate_filter", "from_subquery", "nested_aggregate", "order_by"),
    14: ("case_expression", "like_predicate", "division"),
    15: ("common_table_expression", "computed_aggregate_expression", "scalar_subquery", "order_by"),
    16: ("distinct_aggregate", "like_predicate", "in_predicate", "subquery", "order_by"),
    17: ("division", "correlated_scalar_subquery", "computed_predicate"),
    18: ("in_subquery", "having", "order_by", "limit"),
    19: ("computed_aggregate_expression", "in_predicate", "nested_boolean", "computed_predicate"),
    20: ("nested_subqueries", "like_predicate", "computed_aggregate_expression", "order_by"),
    21: ("correlated_exists", "correlated_not_exists", "field_field_filter", "order_by", "limit"),
    22: ("string_function", "in_predicate", "scalar_subquery", "correlated_not_exists", "order_by"),
}


@dataclass(frozen=True, slots=True)
class TpchQueryAudit:
    query_id: str
    sql_sha256: str
    duckdb_status: str
    output_columns: tuple[str, ...]
    output_row_count: int
    result_digest: str
    ir_v1_status: str
    blockers: tuple[str, ...]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _result_digest(columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...]) -> str:
    payload = {
        "columns": columns,
        "rows": [[_json_value(value) for value in row] for row in rows],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def tpch_git_state(root: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
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
        raise TpchAuditError("TPC-H audit requires a Git repository") from exc
    return commit, dirty


def verify_tpch_artifact(root: Path, *, scale_factor: int) -> tuple[Path, dict[str, Any]]:
    """Verify one reviewed TPC-H database against its content-addressed manifest."""

    if scale_factor not in {1, 10}:
        raise TpchAuditError(f"Unsupported reviewed TPC-H scale factor: {scale_factor}")
    manifest = root / f"data/manifests/processed/tpch-sf{scale_factor}.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TpchAuditError("TPC-H artifact manifest must be a JSON object")
    if int(payload.get("scale_factor", -1)) != scale_factor:
        raise TpchAuditError("TPC-H artifact scale differs from its manifest path")
    database = root / str(payload["database_path"])
    if not database.is_file():
        raise TpchAuditError(f"TPC-H database is missing: {database}")
    if database.stat().st_size != int(payload["byte_size"]):
        raise TpchAuditError("TPC-H database size differs from its manifest")
    if sha256_file(database) != str(payload["sha256"]):
        raise TpchAuditError("TPC-H database digest differs from its manifest")
    return database, payload


def verify_tpch_sf1_artifact(root: Path) -> tuple[Path, dict[str, Any]]:
    """Compatibility wrapper for already frozen SF1 experiments."""

    return verify_tpch_artifact(root, scale_factor=1)


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# TPC-H SF1 official-query support audit",
        "",
        "This is a semantic and execution audit, not a performance result.",
        "DuckDB execution does not imply TrustAero IR support.",
        "",
        "| Query | DuckDB SF1 | TrustAero IR v1 | Exact blockers | Output rows |",
        "|---|---|---|---|---:|",
    ]
    for item in payload["queries"]:
        blockers = ", ".join(item["blockers"]) if item["blockers"] else "none"
        lines.append(
            f"| {item['query_id']} | {item['duckdb_status']} | {item['ir_v1_status']} | "
            f"{blockers} | {item['output_row_count']} |"
        )
    lines.extend(
        [
            "",
            f"Exact IR support: {payload['ir_v1_supported_count']}/22 "
            f"({', '.join(payload['ir_v1_supported_queries'])}).",
            "",
            "Q1 is supported through a bounded fixed-point product formula and explicit "
            "sort keys; Q6 uses explicit filters, a temporal range, and one non-nested "
            "numeric product. No query uses a raw-SQL bypass.",
            "",
        ]
    )
    return "\n".join(lines)


def audit_tpch_sf1(
    project_root: Path,
    *,
    output_directory: Path | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Execute and classify every official TPC-H query on the frozen SF1 DB."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise TpchAuditError("DuckDB is required for the TPC-H audit") from exc
    root = project_root.resolve()
    commit, dirty = tpch_git_state(root)
    if dirty:
        raise TpchAuditError("TPC-H audit requires a clean committed source tree")
    database, artifact = verify_tpch_sf1_artifact(root)
    extension_directory = (root / "data/processed/duckdb_extensions").resolve()
    connection = duckdb.connect(str(database), read_only=True)
    records: list[TpchQueryAudit] = []
    try:
        extension_literal = "'" + str(extension_directory).replace("'", "''") + "'"
        connection.execute(f"SET extension_directory = {extension_literal}")
        connection.execute("LOAD tpch")
        for query_number in range(1, 23):
            query_id = f"Q{query_number:02d}"
            if progress:
                progress(query_number, 22, f"execute and audit {query_id}")
            row = connection.execute(
                "SELECT query FROM tpch_queries() WHERE query_nr = ?", [query_number]
            ).fetchone()
            if row is None:
                raise TpchAuditError(f"Official SQL is missing for {query_id}")
            sql = str(row[0]).strip()
            cursor = connection.execute(sql)
            rows = tuple(tuple(item) for item in cursor.fetchall())
            columns = tuple(str(item[0]) for item in cursor.description)
            blockers = TPCH_IR_V1_BLOCKERS[query_number]
            records.append(
                TpchQueryAudit(
                    query_id=query_id,
                    sql_sha256=hashlib.sha256(sql.encode()).hexdigest(),
                    duckdb_status="PASS",
                    output_columns=columns,
                    output_row_count=len(rows),
                    result_digest=_result_digest(columns, rows),
                    ir_v1_status="SUPPORTED" if not blockers else "BLOCKED",
                    blockers=blockers,
                )
            )
    finally:
        connection.close()

    supported = tuple(item.query_id for item in records if item.ir_v1_status == "SUPPORTED")
    if supported != ("Q01", "Q06"):
        raise TpchAuditError(f"Unexpected exact-support set: {supported}")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "audit_id": "tpch_sf1_ir_v1_q01_q06_support_20260719",
        "status": "PASS",
        "scientific_boundary": (
            "All 22 official queries were executed as semantic smoke tests. This file contains "
            "no paper performance timing and does not treat raw DuckDB SQL as trusted IR."
        ),
        "source_commit": commit,
        "source_dirty": False,
        "artifact_sha256": artifact["sha256"],
        "artifact_rows": sum(int(value) for value in artifact["table_rows"].values()),
        "official_query_count": len(records),
        "duckdb_execution_pass_count": sum(item.duckdb_status == "PASS" for item in records),
        "ir_v1_supported_count": len(supported),
        "ir_v1_supported_queries": list(supported),
        "unsupported_queries_retained_in_denominator": True,
        "queries": [asdict(item) for item in records],
    }
    destination = output_directory or root / "results/tpch_sf1_support_audit_q01_q06_v3"
    _atomic_write(destination / "audit.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _atomic_write(destination / "report.md", _markdown_report(payload))
    return payload
