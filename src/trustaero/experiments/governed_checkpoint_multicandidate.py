"""Three-candidate semantics for governed checkpoint planning.

This module adds the strong baseline that the original two-candidate discovery
experiment intentionally omitted: a fused pipeline with no intermediate
checkpoint.  The three plans compute the same governed aggregate:

* ``fused`` lets DuckDB pipeline both predicates into the Join;
* ``policy_first`` writes a narrow policy-filtered checkpoint;
* ``query_first`` writes a query-filtered checkpoint containing raw values.

The fused plan is not always legal.  A policy may require a durable governance
checkpoint, while another policy may forbid raw values in that checkpoint.
Those are hard feasibility decisions made before any cost comparison.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trustaero.experiments.governed_checkpoint_reversal import (
    POLICY_FIRST,
    QUERY_FIRST,
    GovernedCheckpointUnit,
)
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    CandidateFeasibilityResult,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)

FUSED = "fused_governed_pipeline"
MULTICANDIDATE_IDS = (FUSED, POLICY_FIRST, QUERY_FIRST)


@dataclass(frozen=True, slots=True)
class GovernedCheckpointCandidate:
    """Executable SQL and trusted governance metadata for one candidate."""

    candidate_id: str
    checkpoint_sql: str | None
    output_sql: str
    exposure: CandidateExposure
    materialization_kind: str


def build_governed_checkpoint_candidate(
    candidate_id: str,
    unit: GovernedCheckpointUnit,
    actual_cardinalities: Mapping[str, int],
) -> GovernedCheckpointCandidate:
    """Build one result-equivalent candidate from trusted workload statistics."""

    policy_predicate = f"hash(sensitive_value) % 10000 < {unit.policy_cutoff}"
    query_predicate = f"query_bucket < {unit.query_cutoff}"
    if candidate_id == FUSED:
        # The sensitive value is consumed by the policy predicate and is not
        # projected into the Join.  There is no durable intermediate table.
        output = f"""
            CREATE TEMP TABLE governed_output AS
            SELECT
                count(*)::BIGINT AS result_rows,
                sum(event.row_id)::HUGEINT AS row_id_sum,
                sum(dimension.marker)::HUGEINT AS marker_sum
            FROM events AS event
            INNER JOIN dimension
              ON event.join_key = dimension.dimension_key
            WHERE event.{query_predicate}
              AND hash(event.sensitive_value) % 10000 < {unit.policy_cutoff}
        """
        return GovernedCheckpointCandidate(
            candidate_id=candidate_id,
            checkpoint_sql=None,
            output_sql=output,
            exposure=CandidateExposure(
                candidate_id,
                raw_rows_exposed_to_join=0,
                raw_rows_materialized=0,
                provides_governance_checkpoint=False,
            ),
            materialization_kind="none",
        )
    if candidate_id == POLICY_FIRST:
        checkpoint = f"""
            CREATE TEMP TABLE governance_checkpoint AS
            SELECT row_id, join_key, query_bucket
            FROM events
            WHERE {policy_predicate}
        """
        output_predicate = f"checkpoint.{query_predicate}"
        raw_rows = 0
        materialization_kind = "narrow_policy_checkpoint"
    elif candidate_id == QUERY_FIRST:
        checkpoint = f"""
            CREATE TEMP TABLE governance_checkpoint AS
            SELECT row_id, join_key, sensitive_value
            FROM events
            WHERE {query_predicate}
        """
        output_predicate = f"hash(checkpoint.sensitive_value) % 10000 < {unit.policy_cutoff}"
        raw_rows = int(actual_cardinalities["query_rows"])
        materialization_kind = "raw_query_checkpoint"
    else:
        raise ValueError(f"Unknown governed multi-candidate plan: {candidate_id}")

    output = f"""
        CREATE TEMP TABLE governed_output AS
        SELECT
            count(*)::BIGINT AS result_rows,
            sum(checkpoint.row_id)::HUGEINT AS row_id_sum,
            sum(dimension.marker)::HUGEINT AS marker_sum
        FROM governance_checkpoint AS checkpoint
        INNER JOIN dimension
          ON checkpoint.join_key = dimension.dimension_key
        WHERE {output_predicate}
    """
    return GovernedCheckpointCandidate(
        candidate_id=candidate_id,
        checkpoint_sql=checkpoint,
        output_sql=output,
        exposure=CandidateExposure(
            candidate_id,
            raw_rows_exposed_to_join=0,
            raw_rows_materialized=raw_rows,
            provides_governance_checkpoint=True,
        ),
        materialization_kind=materialization_kind,
    )


def checkpoint_candidate_feasibility(
    unit: GovernedCheckpointUnit,
    actual_cardinalities: Mapping[str, int],
    policy: GovernanceFeasibilityPolicy,
) -> CandidateFeasibilityResult:
    """Filter all three candidates before a later optimizer sees their cost."""

    exposures = tuple(
        build_governed_checkpoint_candidate(
            candidate_id,
            unit,
            actual_cardinalities,
        ).exposure
        for candidate_id in MULTICANDIDATE_IDS
    )
    return filter_feasible_candidates(exposures, policy)


def execute_governed_checkpoint_candidate(
    connection: Any,
    candidate: GovernedCheckpointCandidate,
) -> tuple[object, ...]:
    """Execute one candidate and return a comparable result checksum tuple."""

    connection.execute("DROP TABLE IF EXISTS governed_output")
    connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
    if candidate.checkpoint_sql is not None:
        connection.execute(candidate.checkpoint_sql)
    connection.execute(candidate.output_sql)
    row = connection.execute(
        "SELECT result_rows, row_id_sum, marker_sum FROM governed_output"
    ).fetchone()
    if row is None:
        raise ValueError("Governed multi-candidate plan returned no result")
    return tuple(row)


def result_digest(checksum: tuple[object, ...]) -> str:
    """Create a stable digest without exposing result contents in reports."""

    encoded = json.dumps(checksum, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
