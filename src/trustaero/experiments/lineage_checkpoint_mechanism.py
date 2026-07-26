"""Executable semantics for record-lineage checkpoint placement.

This module deliberately omits a cost model.  It first proves that three
physical strategies return identical visible rows and ordinal-bound source
identities for every query in a batch.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from trustaero.execution import observe_duckdb_plan
from trustaero.optimizer.lineage_checkpoint_space import (
    LATE_PER_QUERY_CAPTURE,
    LINEAGE_CHECKPOINT_CANDIDATE_IDS,
    POLICY_LINEAGE_CHECKPOINT,
    SNAPSHOT_LINEAGE_CHECKPOINT,
)


@dataclass(frozen=True, slots=True)
class LineageBatchQuery:
    """One policy/query selection over the same frozen source snapshot."""

    query_id: str
    policy_cutoff: int
    query_cutoff: int

    def __post_init__(self) -> None:
        if not self.query_id:
            raise ValueError("Lineage batch query ID cannot be empty")
        if not 0 < self.policy_cutoff <= 10_000:
            raise ValueError("Policy cutoff must be in (0, 10000]")
        if not 0 < self.query_cutoff <= 10_000:
            raise ValueError("Query cutoff must be in (0, 10000]")


@dataclass(frozen=True, slots=True)
class LineageBatchQueryEvidence:
    """Result and record-lineage commitments for one query."""

    query_id: str
    row_count: int
    result_digest: str
    edge_digest: str
    evidence_bytes: int


@dataclass(frozen=True, slots=True)
class LineageCheckpointExecution:
    """Observed batch evidence and measured total database work."""

    candidate_id: str
    latency_ms: float
    checkpoint_rows: int
    query_evidence: tuple[LineageBatchQueryEvidence, ...]


def _source_identity_sql(alias: str) -> str:
    """Match V4's length-delimited 32-byte source identity design."""

    key = f"{alias}.event_id"
    length = f"CAST(octet_length(encode({key})) AS VARCHAR)"
    return f"unhex(sha256('lineage-checkpoint-v1|' || {length} || ':' || {key}))"


def _digest(value: object) -> str:
    payload = json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _drop_checkpoints(connection: Any) -> None:
    rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_name = 'snapshot_lineage_checkpoint'
           OR table_name LIKE 'policy_lineage_checkpoint_%'
        """
    ).fetchall()
    for (table_name,) in rows:
        connection.execute(f'DROP TABLE IF EXISTS "{table_name}"')


def _query_sql(
    candidate_id: str,
    query: LineageBatchQuery,
) -> str:
    if candidate_id == LATE_PER_QUERY_CAPTURE:
        return f"""
            SELECT source.event_value, source.query_bucket,
                   {_source_identity_sql("source")} AS source_identity
            FROM lineage_events AS source
            WHERE source.policy_bucket < {query.policy_cutoff}
              AND source.query_bucket < {query.query_cutoff}
            ORDER BY source_identity
        """
    if candidate_id == POLICY_LINEAGE_CHECKPOINT:
        return f"""
            SELECT checkpoint.event_value, checkpoint.query_bucket,
                   checkpoint.source_identity
            FROM policy_lineage_checkpoint_{query.policy_cutoff} AS checkpoint
            WHERE checkpoint.query_bucket < {query.query_cutoff}
            ORDER BY checkpoint.source_identity
        """
    if candidate_id == SNAPSHOT_LINEAGE_CHECKPOINT:
        return f"""
            SELECT checkpoint.event_value, checkpoint.query_bucket,
                   checkpoint.source_identity
            FROM snapshot_lineage_checkpoint AS checkpoint
            WHERE checkpoint.policy_bucket < {query.policy_cutoff}
              AND checkpoint.query_bucket < {query.query_cutoff}
            ORDER BY checkpoint.source_identity
        """
    raise ValueError(f"Unknown lineage checkpoint candidate: {candidate_id}")


def lineage_checkpoint_setup_sql(
    candidate_id: str,
    queries: tuple[LineageBatchQuery, ...],
) -> tuple[str, ...]:
    """Return explicit checkpoint DDL without executing it."""

    if candidate_id == LATE_PER_QUERY_CAPTURE:
        return ()
    if candidate_id == POLICY_LINEAGE_CHECKPOINT:
        return tuple(
            f"""
            CREATE TEMP TABLE policy_lineage_checkpoint_{cutoff} AS
            SELECT source.event_value, source.query_bucket,
                   {_source_identity_sql("source")} AS source_identity
            FROM lineage_events AS source
            WHERE source.policy_bucket < {cutoff}
            """
            for cutoff in sorted({query.policy_cutoff for query in queries})
        )
    if candidate_id == SNAPSHOT_LINEAGE_CHECKPOINT:
        return (
            f"""
            CREATE TEMP TABLE snapshot_lineage_checkpoint AS
            SELECT source.policy_bucket, source.query_bucket, source.event_value,
                   {_source_identity_sql("source")} AS source_identity
            FROM lineage_events AS source
            """,
        )
    raise ValueError(f"Unknown lineage checkpoint candidate: {candidate_id}")


def observe_lineage_checkpoint_plan(
    connection: Any,
    candidate_id: str,
    queries: tuple[LineageBatchQuery, ...],
) -> str:
    """Fingerprint every checkpoint phase and representative batch query."""

    _drop_checkpoints(connection)
    fingerprints: list[str] = []
    for statement in lineage_checkpoint_setup_sql(candidate_id, queries):
        fingerprints.append(observe_duckdb_plan(connection, statement, analyze=False).fingerprint)
        connection.execute(statement)
    for query in queries:
        fingerprints.append(
            observe_duckdb_plan(
                connection,
                _query_sql(candidate_id, query),
                analyze=False,
            ).fingerprint
        )
    _drop_checkpoints(connection)
    return hashlib.sha256("|".join(fingerprints).encode()).hexdigest()


def execute_lineage_checkpoint_batch(
    connection: Any,
    candidate_id: str,
    queries: tuple[LineageBatchQuery, ...],
) -> LineageCheckpointExecution:
    """Execute one candidate and derive identical V4-style evidence bytes."""

    if candidate_id not in LINEAGE_CHECKPOINT_CANDIDATE_IDS:
        raise ValueError(f"Unknown lineage checkpoint candidate: {candidate_id}")
    if not queries:
        raise ValueError("Lineage checkpoint batch cannot be empty")
    _drop_checkpoints(connection)
    started = time.perf_counter()
    checkpoint_rows = 0
    setup_sql = lineage_checkpoint_setup_sql(candidate_id, queries)
    for statement in setup_sql:
        connection.execute(statement)
    if candidate_id == POLICY_LINEAGE_CHECKPOINT:
        for cutoff in sorted({query.policy_cutoff for query in queries}):
            checkpoint_rows += int(
                connection.execute(
                    f"SELECT count(*) FROM policy_lineage_checkpoint_{cutoff}"
                ).fetchone()[0]
            )
    elif candidate_id == SNAPSHOT_LINEAGE_CHECKPOINT:
        checkpoint_rows = int(
            connection.execute("SELECT count(*) FROM snapshot_lineage_checkpoint").fetchone()[0]
        )

    evidence: list[LineageBatchQueryEvidence] = []
    for query in queries:
        rows = tuple(connection.execute(_query_sql(candidate_id, query)).fetchall())
        visible_rows = tuple(row[:-1] for row in rows)
        source_ids = b"".join(row[-1] for row in rows)
        if len(source_ids) != len(rows) * 32:
            raise ValueError("Lineage checkpoint emitted a non-32-byte source identity")
        evidence.append(
            LineageBatchQueryEvidence(
                query_id=query.query_id,
                row_count=len(rows),
                result_digest=_digest(visible_rows),
                edge_digest="sha256:" + hashlib.sha256(source_ids).hexdigest(),
                evidence_bytes=len(source_ids),
            )
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    _drop_checkpoints(connection)
    return LineageCheckpointExecution(
        candidate_id=candidate_id,
        latency_ms=latency_ms,
        checkpoint_rows=checkpoint_rows,
        query_evidence=tuple(evidence),
    )
