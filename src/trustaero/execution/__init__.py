"""Trusted execution helpers for the first backend-facing TrustAero fragment."""

from trustaero.execution.compiler import (
    CompiledQuery,
    ExecutionCompileError,
    TableBindings,
    compile_validated_plan,
)
from trustaero.execution.duckdb import (
    DuckDBUnavailable,
    QueryExecutionResult,
    execute_with_connection,
    execute_with_duckdb,
)

__all__ = [
    "CompiledQuery",
    "DuckDBUnavailable",
    "ExecutionCompileError",
    "QueryExecutionResult",
    "TableBindings",
    "compile_validated_plan",
    "execute_with_connection",
    "execute_with_duckdb",
]
