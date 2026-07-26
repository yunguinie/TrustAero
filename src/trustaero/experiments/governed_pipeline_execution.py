"""Executable DuckDB semantics for governed multi-operator candidates.

Every materialization boundary is explicit.  Result and record-lineage digests
are derived from observed rows, so candidate metadata cannot self-certify
equivalence or provenance.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.governed_checkpoint_reversal import (
    GovernedCheckpointUnit,
)
from trustaero.optimizer.governed_pipeline_space import (
    FUSED_GOVERNED,
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
)


@dataclass(frozen=True, slots=True)
class ExecutableGovernedPipeline:
    """SQL phases for one trusted candidate template."""

    candidate_id: str
    setup_sql: tuple[str, ...]
    output_sql: str
    temporary_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GovernedPipelineExecutionEvidence:
    """Observed result equality and record-lineage evidence."""

    candidate_id: str
    row_count: int
    result_digest: str
    lineage_digest: str
    lineage_capture_latency_ms: float
    source_event_count: int
    source_dimension_count: int


@dataclass(frozen=True, slots=True)
class GovernedPipelinePlanEvidence:
    """Combined DuckDB fingerprint across all candidate phases."""

    candidate_id: str
    phase_fingerprints: tuple[str, ...]
    combined_fingerprint: str
    operator_names: tuple[str, ...]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_executable_governed_pipeline(
    candidate_id: str,
    unit: GovernedCheckpointUnit,
) -> ExecutableGovernedPipeline:
    """Compile one candidate into explicit result-equivalent DuckDB phases."""

    policy = f"hash(sensitive_value) % 10000 < {unit.policy_cutoff}"
    query = f"query_bucket < {unit.query_cutoff}"
    if candidate_id == FUSED_GOVERNED:
        return ExecutableGovernedPipeline(
            candidate_id,
            (),
            f"""
                SELECT event.row_id, dimension.dimension_key,
                       dimension.marker,
                       md5(event.sensitive_value) AS masked_value
                FROM events AS event
                INNER JOIN dimension
                  ON event.join_key = dimension.dimension_key
                WHERE event.{query}
                  AND hash(event.sensitive_value) % 10000
                      < {unit.policy_cutoff}
                ORDER BY event.row_id, dimension.dimension_key
            """,
            (),
        )
    if candidate_id == POLICY_FIRST_MASKED_CHECKPOINT:
        return ExecutableGovernedPipeline(
            candidate_id,
            (
                f"""
                    CREATE TEMP TABLE pipeline_checkpoint AS
                    SELECT row_id, join_key, query_bucket,
                           md5(sensitive_value) AS masked_value
                    FROM events WHERE {policy}
                """,
            ),
            f"""
                SELECT checkpoint.row_id, dimension.dimension_key,
                       dimension.marker, checkpoint.masked_value
                FROM pipeline_checkpoint AS checkpoint
                INNER JOIN dimension
                  ON checkpoint.join_key = dimension.dimension_key
                WHERE checkpoint.{query}
                ORDER BY checkpoint.row_id, dimension.dimension_key
            """,
            ("pipeline_checkpoint",),
        )
    if candidate_id == QUERY_FIRST_RAW_CHECKPOINT:
        return ExecutableGovernedPipeline(
            candidate_id,
            (
                f"""
                    CREATE TEMP TABLE raw_query_checkpoint AS
                    SELECT row_id, join_key, sensitive_value
                    FROM events WHERE {query}
                """,
                f"""
                    CREATE TEMP TABLE pipeline_checkpoint AS
                    SELECT row_id, join_key,
                           md5(sensitive_value) AS masked_value
                    FROM raw_query_checkpoint WHERE {policy}
                """,
            ),
            """
                SELECT checkpoint.row_id, dimension.dimension_key,
                       dimension.marker, checkpoint.masked_value
                FROM pipeline_checkpoint AS checkpoint
                INNER JOIN dimension
                  ON checkpoint.join_key = dimension.dimension_key
                ORDER BY checkpoint.row_id, dimension.dimension_key
            """,
            ("raw_query_checkpoint", "pipeline_checkpoint"),
        )
    if candidate_id == JOIN_FIRST_MASKED_CHECKPOINT:
        return ExecutableGovernedPipeline(
            candidate_id,
            (
                f"""
                    CREATE TEMP TABLE raw_join_checkpoint AS
                    SELECT event.row_id, dimension.dimension_key,
                           dimension.marker, event.sensitive_value
                    FROM events AS event
                    INNER JOIN dimension
                      ON event.join_key = dimension.dimension_key
                    WHERE event.{query}
                """,
                f"""
                    CREATE TEMP TABLE pipeline_checkpoint AS
                    SELECT row_id, dimension_key, marker,
                           md5(sensitive_value) AS masked_value
                    FROM raw_join_checkpoint WHERE {policy}
                """,
            ),
            """
                SELECT row_id, dimension_key, marker, masked_value
                FROM pipeline_checkpoint
                ORDER BY row_id, dimension_key
            """,
            ("raw_join_checkpoint", "pipeline_checkpoint"),
        )
    raise ValueError(f"Unknown governed pipeline candidate: {candidate_id}")


def _drop_tables(
    connection: Any,
    candidate: ExecutableGovernedPipeline,
) -> None:
    for table in reversed(candidate.temporary_tables):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def observe_governed_pipeline_plan(
    connection: Any,
    candidate: ExecutableGovernedPipeline,
) -> GovernedPipelinePlanEvidence:
    """Observe every phase and return one reproducible combined fingerprint."""

    _drop_tables(connection, candidate)
    fingerprints: list[str] = []
    operators: list[str] = []
    for index, statement in enumerate(candidate.setup_sql):
        observation = observe_duckdb_plan(connection, statement, analyze=False)
        fingerprints.append(observation.fingerprint)
        operators.extend(observation.operator_names)
        # Profiling DDL differs across DuckDB versions. Remove any possible
        # target, but preserve inputs created by an earlier phase.
        connection.execute(f"DROP TABLE IF EXISTS {candidate.temporary_tables[index]}")
        connection.execute(statement)
    output = observe_duckdb_plan(connection, candidate.output_sql, analyze=False)
    fingerprints.append(output.fingerprint)
    operators.extend(output.operator_names)
    _drop_tables(connection, candidate)
    return GovernedPipelinePlanEvidence(
        candidate_id=candidate.candidate_id,
        phase_fingerprints=tuple(fingerprints),
        combined_fingerprint=_digest(fingerprints),
        operator_names=tuple(operators),
    )


def execute_governed_pipeline(
    connection: Any,
    candidate: ExecutableGovernedPipeline,
) -> GovernedPipelineExecutionEvidence:
    """Execute one candidate and derive record lineage from source identities."""

    _drop_tables(connection, candidate)
    for statement in candidate.setup_sql:
        connection.execute(statement)
    rows = tuple(connection.execute(candidate.output_sql).fetchall())

    started = time.perf_counter()
    # row_id and dimension_key are source identities, not candidate claims.
    lineage_edges = tuple((int(row[0]), int(row[1])) for row in rows)
    lineage_digest = _digest(lineage_edges)
    lineage_latency_ms = (time.perf_counter() - started) * 1000.0
    evidence = GovernedPipelineExecutionEvidence(
        candidate_id=candidate.candidate_id,
        row_count=len(rows),
        result_digest=_digest(rows),
        lineage_digest=lineage_digest,
        lineage_capture_latency_ms=lineage_latency_ms,
        source_event_count=len({edge[0] for edge in lineage_edges}),
        source_dimension_count=len({edge[1] for edge in lineage_edges}),
    )
    _drop_tables(connection, candidate)
    return evidence
