"""Optional DuckDB runner for compiled TrustAero queries.

DuckDB is intentionally optional at package import time. The core validator and
Phase 0 semantic experiments must remain usable without a database dependency.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from trustaero.execution.compiler import CompiledQuery


class DuckDBUnavailable(RuntimeError):
    """Raised when the optional ``duckdb`` package is not installed."""


class DuckDBLikeConnection(Protocol):
    """Small subset of the DuckDB connection API used by this module."""

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> DuckDBLikeConnection: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...


@dataclass(frozen=True)
class QueryExecutionResult:
    """Materialized result summary for later certificate binding."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    result_digest: str


def _jsonable(value: Any) -> Any:
    """Convert DB values into stable JSON-compatible values for hashing."""

    if isinstance(value, Decimal):
        # Keep the fixed-point representation exact; converting currency to a
        # float would reintroduce the digest instability this boundary avoids.
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _result_digest(columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...]) -> str:
    payload = {
        "columns": columns,
        "rows": [[_jsonable(value) for value in row] for row in rows],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def materialize_query_result(
    columns: tuple[str, ...],
    rows: tuple[tuple[Any, ...], ...],
) -> QueryExecutionResult:
    """Build the canonical trusted result object from already fetched rows."""

    if any(len(row) != len(columns) for row in rows):
        raise ValueError("Materialized row width does not match output columns")
    return QueryExecutionResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        result_digest=_result_digest(columns, rows),
    )


def execute_with_connection(
    query: CompiledQuery,
    connection: DuckDBLikeConnection,
) -> QueryExecutionResult:
    """Execute a compiled query using an already configured trusted connection."""

    cursor = connection.execute(query.sql, query.parameters)
    rows = tuple(tuple(row) for row in cursor.fetchall())
    return materialize_query_result(query.output_fields, rows)


def execute_with_duckdb(query: CompiledQuery, database: str = ":memory:") -> QueryExecutionResult:
    """Open DuckDB and execute a compiled query.

    The caller is responsible for creating or loading the physical tables named
    in ``TableBindings`` before calling this helper. For multi-step experiments,
    prefer ``execute_with_connection`` so setup and execution share one trusted
    connection.
    """

    try:
        duckdb: Any = importlib.import_module("duckdb")
    except ModuleNotFoundError as exc:
        raise DuckDBUnavailable("Install TrustAero's optional DuckDB dependency first.") from exc
    connection = duckdb.connect(database)
    try:
        return execute_with_connection(query, connection)
    finally:
        connection.close()
