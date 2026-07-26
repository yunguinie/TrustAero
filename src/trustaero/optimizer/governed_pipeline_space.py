"""Governance-driven multi-operator candidate space for TrustAero.

The older checkpoint experiment varied only two filter orders.  This module
defines a broader result-equivalent family whose candidates move Policy,
Query, Join, Mask, checkpoint materialization, and record-lineage work
together.  The differences are governance meaningful:

* policy-first avoids every raw-value exposure but evaluates policy early;
* query-first reduces policy work but writes a raw checkpoint;
* join-first reduces policy work further but lets raw values reach the Join;
* fused avoids a durable checkpoint and is legal only when policy permits it.

No latency model is fitted here.  The output is trusted candidate metadata for
legality filtering, safe dominance pruning, and later experimental admission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.hierarchical_planner import (
    GovernedCandidateProfile,
    HierarchicalPlannerConfig,
    HierarchicalPlanningResult,
    plan_governed_candidates,
)

FUSED_GOVERNED = "fused_governed"
POLICY_FIRST_MASKED_CHECKPOINT = "policy_first_masked_checkpoint"
QUERY_FIRST_RAW_CHECKPOINT = "query_first_raw_checkpoint"
JOIN_FIRST_MASKED_CHECKPOINT = "join_first_masked_checkpoint"

GOVERNED_PIPELINE_CANDIDATE_IDS = (
    FUSED_GOVERNED,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    JOIN_FIRST_MASKED_CHECKPOINT,
)
GOVERNED_PIPELINE_EQUIVALENCE = "governed-masked-output-with-record-lineage-v1"
MASKED_VALUE_WIDTH_BYTES = 64.0


@dataclass(frozen=True, slots=True)
class GovernedPipelineStatistics:
    """Trusted cardinality estimates shared by every candidate."""

    input_rows: int
    estimated_policy_rows: int
    estimated_query_rows: int
    estimated_governed_rows: int
    estimated_query_join_rows: int
    estimated_result_rows: int
    sensitive_width_bytes: float

    def __post_init__(self) -> None:
        counts = (
            self.input_rows,
            self.estimated_policy_rows,
            self.estimated_query_rows,
            self.estimated_governed_rows,
            self.estimated_query_join_rows,
            self.estimated_result_rows,
        )
        if self.input_rows <= 0 or any(value < 0 for value in counts):
            raise ValueError("Governed pipeline row counts are invalid")
        if max(counts[1:]) > self.input_rows:
            raise ValueError("Governed pipeline estimates exceed input rows")
        if self.estimated_governed_rows > min(
            self.estimated_policy_rows,
            self.estimated_query_rows,
        ):
            raise ValueError("Governed rows exceed a filtering input")
        if self.estimated_query_join_rows > self.estimated_query_rows:
            raise ValueError("Query Join rows exceed query-filtered rows")
        if self.estimated_result_rows > min(
            self.estimated_governed_rows,
            self.estimated_query_join_rows,
        ):
            raise ValueError("Final result rows exceed governed Join inputs")
        if self.sensitive_width_bytes <= 0.0 or not math.isfinite(self.sensitive_width_bytes):
            raise ValueError("Sensitive width must be finite and positive")


@dataclass(frozen=True, slots=True)
class GovernedPipelineCandidate:
    """One semantically explicit physical candidate before cost ranking."""

    candidate_id: str
    operator_order: tuple[str, ...]
    checkpoint_kind: str
    lineage_capture_point: str
    profile: GovernedCandidateProfile

    def __post_init__(self) -> None:
        if self.profile.candidate_id != self.candidate_id:
            raise ValueError("Pipeline profile must be bound to its candidate ID")
        if len(self.operator_order) != len(set(self.operator_order)):
            raise ValueError("Pipeline operator stages must be unique")
        if self.operator_order[-1] != "ProjectMaskedResult":
            raise ValueError("Every candidate must return the same masked result")
        if "RecordLineage" not in self.operator_order:
            raise ValueError("Every candidate must provide record lineage")


def _metrics(
    *,
    checkpoint_rows: int,
    checkpoint_payload_bytes: float,
    join_probe_rows: int,
    mask_rows: int,
    policy_hash_rows: int,
    sensitive_width_bytes: float,
    result_rows: int,
    pipeline_breakers: int,
) -> tuple[tuple[str, float], ...]:
    """Create one common metric schema without converting work into latency."""

    return (
        ("checkpoint.payload_bytes", checkpoint_payload_bytes),
        ("checkpoint.rows", float(checkpoint_rows)),
        ("join.probe_rows", float(join_probe_rows)),
        ("lineage.edges", float(result_rows * 2)),
        ("lineage.rows", float(result_rows)),
        ("mask.input_bytes", mask_rows * sensitive_width_bytes),
        ("mask.rows", float(mask_rows)),
        ("pipeline_breaker.count", float(pipeline_breakers)),
        ("policy_hash.input_bytes", policy_hash_rows * sensitive_width_bytes),
        ("policy_hash.rows", float(policy_hash_rows)),
    )


def build_governed_pipeline_candidates(
    statistics: GovernedPipelineStatistics,
) -> tuple[GovernedPipelineCandidate, ...]:
    """Build four result-equivalent candidates with truthful exposure."""

    width = statistics.sensitive_width_bytes
    result_rows = statistics.estimated_result_rows

    def candidate(
        candidate_id: str,
        order: tuple[str, ...],
        checkpoint_kind: str,
        exposure: CandidateExposure,
        metrics: tuple[tuple[str, float], ...],
    ) -> GovernedPipelineCandidate:
        return GovernedPipelineCandidate(
            candidate_id=candidate_id,
            operator_order=order,
            checkpoint_kind=checkpoint_kind,
            lineage_capture_point="result",
            profile=GovernedCandidateProfile(
                candidate_id=candidate_id,
                result_equivalence_id=GOVERNED_PIPELINE_EQUIVALENCE,
                exposure=exposure,
                work_metrics=metrics,
            ),
        )

    return (
        candidate(
            FUSED_GOVERNED,
            (
                "Scan",
                "FusedQueryPolicyFilter",
                "Join",
                "Mask",
                "RecordLineage",
                "ProjectMaskedResult",
            ),
            "none",
            CandidateExposure(
                FUSED_GOVERNED,
                0,
                0,
                provides_governance_checkpoint=False,
            ),
            _metrics(
                checkpoint_rows=0,
                checkpoint_payload_bytes=0.0,
                join_probe_rows=statistics.estimated_governed_rows,
                mask_rows=result_rows,
                policy_hash_rows=statistics.input_rows,
                sensitive_width_bytes=width,
                result_rows=result_rows,
                pipeline_breakers=0,
            ),
        ),
        candidate(
            POLICY_FIRST_MASKED_CHECKPOINT,
            (
                "Scan",
                "PolicyFilter",
                "Mask",
                "MaskedCheckpoint",
                "QueryFilter",
                "Join",
                "RecordLineage",
                "ProjectMaskedResult",
            ),
            "masked",
            CandidateExposure(
                POLICY_FIRST_MASKED_CHECKPOINT,
                0,
                0,
                masked_rows_materialized=statistics.estimated_policy_rows,
            ),
            _metrics(
                checkpoint_rows=statistics.estimated_policy_rows,
                checkpoint_payload_bytes=(
                    statistics.estimated_policy_rows * MASKED_VALUE_WIDTH_BYTES
                ),
                join_probe_rows=statistics.estimated_governed_rows,
                mask_rows=statistics.estimated_policy_rows,
                policy_hash_rows=statistics.input_rows,
                sensitive_width_bytes=width,
                result_rows=result_rows,
                pipeline_breakers=1,
            ),
        ),
        candidate(
            QUERY_FIRST_RAW_CHECKPOINT,
            (
                "Scan",
                "QueryFilter",
                "RawCheckpoint",
                "PolicyFilter",
                "Mask",
                "MaskedCheckpoint",
                "Join",
                "RecordLineage",
                "ProjectMaskedResult",
            ),
            "raw_then_masked",
            CandidateExposure(
                QUERY_FIRST_RAW_CHECKPOINT,
                0,
                statistics.estimated_query_rows,
                masked_rows_materialized=statistics.estimated_governed_rows,
            ),
            _metrics(
                checkpoint_rows=(
                    statistics.estimated_query_rows + statistics.estimated_governed_rows
                ),
                checkpoint_payload_bytes=(
                    statistics.estimated_query_rows * (16.0 + width)
                    + statistics.estimated_governed_rows * MASKED_VALUE_WIDTH_BYTES
                ),
                join_probe_rows=statistics.estimated_governed_rows,
                mask_rows=statistics.estimated_governed_rows,
                policy_hash_rows=statistics.estimated_query_rows,
                sensitive_width_bytes=width,
                result_rows=result_rows,
                pipeline_breakers=2,
            ),
        ),
        candidate(
            JOIN_FIRST_MASKED_CHECKPOINT,
            (
                "Scan",
                "QueryFilter",
                "JoinRawSensitive",
                "RawJoinCheckpoint",
                "PolicyFilter",
                "Mask",
                "MaskedCheckpoint",
                "RecordLineage",
                "ProjectMaskedResult",
            ),
            "raw_join_then_masked",
            CandidateExposure(
                JOIN_FIRST_MASKED_CHECKPOINT,
                statistics.estimated_query_rows,
                statistics.estimated_query_join_rows,
                masked_rows_materialized=result_rows,
            ),
            _metrics(
                checkpoint_rows=(statistics.estimated_query_join_rows + result_rows),
                checkpoint_payload_bytes=(
                    statistics.estimated_query_join_rows * (24.0 + width)
                    + result_rows * MASKED_VALUE_WIDTH_BYTES
                ),
                join_probe_rows=statistics.estimated_query_rows,
                mask_rows=result_rows,
                policy_hash_rows=statistics.estimated_query_join_rows,
                sensitive_width_bytes=width,
                result_rows=result_rows,
                pipeline_breakers=2,
            ),
        ),
    )


def plan_governed_pipeline(
    statistics: GovernedPipelineStatistics,
    policy: GovernanceFeasibilityPolicy,
) -> HierarchicalPlanningResult:
    """Apply governance and dominance before any future cost model."""

    candidates = build_governed_pipeline_candidates(statistics)
    fallback = (
        POLICY_FIRST_MASKED_CHECKPOINT if policy.require_governance_checkpoint else FUSED_GOVERNED
    )
    return plan_governed_candidates(
        tuple(candidate.profile for candidate in candidates),
        policy,
        HierarchicalPlannerConfig(
            conservative_fallback_candidate_id=fallback,
        ),
    )
