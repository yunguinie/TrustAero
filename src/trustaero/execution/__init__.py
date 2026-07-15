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
from trustaero.execution.explain import PhysicalPlanObservation, observe_duckdb_plan
from trustaero.execution.lineage import (
    LineageInstrumentationError,
    SourceLineageCaptureResult,
    capture_source_lineage,
)

__all__ = [
    "CompiledQuery",
    "DuckDBUnavailable",
    "ExecutionCompileError",
    "LineageInstrumentationError",
    "PhysicalPlanObservation",
    "QueryExecutionResult",
    "TableBindings",
    "SourceLineageCaptureResult",
    "capture_source_lineage",
    "compile_validated_plan",
    "execute_with_connection",
    "execute_with_duckdb",
    "observe_duckdb_plan",
]
