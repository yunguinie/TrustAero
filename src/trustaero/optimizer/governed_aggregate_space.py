"""Legal candidate space for governed Join-Aggregate placement.

Both candidates satisfy the same no-raw-join policy and result equivalence.
Their work vectors expose a real tradeoff: partial aggregation reduces Join
rows but adds a materialization boundary and extra aggregate work.
"""

from __future__ import annotations

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

JOIN_THEN_AGGREGATE = "join_then_aggregate"
PARTIAL_AGGREGATE_THEN_JOIN = "partial_aggregate_then_join"
AGGREGATE_CANDIDATE_IDS = (
    JOIN_THEN_AGGREGATE,
    PARTIAL_AGGREGATE_THEN_JOIN,
)
AGGREGATE_EQUIVALENCE_ID = "governed-marker-count-with-record-lineage-v1"


@dataclass(frozen=True, slots=True)
class GovernedAggregateStatistics:
    """Observed or estimated work shared by both aggregate candidates."""

    governed_rows: int
    governed_keys: int
    masked_width_bytes: float = 64.0

    def __post_init__(self) -> None:
        if self.governed_rows <= 0:
            raise ValueError("Aggregate planning requires governed rows")
        if not 0 < self.governed_keys <= self.governed_rows:
            raise ValueError("Governed key count must be within governed rows")
        if self.masked_width_bytes <= 0.0:
            raise ValueError("Masked width must be positive")


def build_governed_aggregate_profiles(
    statistics: GovernedAggregateStatistics,
) -> tuple[GovernedCandidateProfile, ...]:
    """Build comparable physical-work vectors with truthful exposure."""

    rows = float(statistics.governed_rows)
    keys = float(statistics.governed_keys)
    checkpoint_bytes = rows * (16.0 + statistics.masked_width_bytes)
    common_exposure = CandidateExposure(
        candidate_id=JOIN_THEN_AGGREGATE,
        raw_rows_exposed_to_join=0,
        raw_rows_materialized=0,
        masked_rows_materialized=statistics.governed_rows,
    )
    join_then = GovernedCandidateProfile(
        candidate_id=JOIN_THEN_AGGREGATE,
        result_equivalence_id=AGGREGATE_EQUIVALENCE_ID,
        exposure=common_exposure,
        work_metrics=(
            ("aggregate.input_rows", rows),
            ("checkpoint.payload_bytes", checkpoint_bytes),
            ("checkpoint.rows", rows),
            ("join.probe_rows", rows),
            ("lineage.rows", rows),
            ("pipeline_breaker.count", 1.0),
        ),
    )
    partial_then_join = GovernedCandidateProfile(
        candidate_id=PARTIAL_AGGREGATE_THEN_JOIN,
        result_equivalence_id=AGGREGATE_EQUIVALENCE_ID,
        exposure=CandidateExposure(
            candidate_id=PARTIAL_AGGREGATE_THEN_JOIN,
            raw_rows_exposed_to_join=0,
            raw_rows_materialized=0,
            masked_rows_materialized=statistics.governed_rows,
        ),
        work_metrics=(
            ("aggregate.input_rows", rows + keys),
            ("checkpoint.payload_bytes", checkpoint_bytes + keys * 24.0),
            ("checkpoint.rows", rows + keys),
            ("join.probe_rows", keys),
            ("lineage.rows", rows),
            ("pipeline_breaker.count", 2.0),
        ),
    )
    return join_then, partial_then_join


def plan_governed_aggregate(
    statistics: GovernedAggregateStatistics,
) -> HierarchicalPlanningResult:
    """Filter illegal candidates before any latency observation."""

    profiles = build_governed_aggregate_profiles(statistics)
    policy = GovernanceFeasibilityPolicy(
        policy_id="no-raw-join-aggregate",
        max_raw_join_rows=0,
        max_raw_materialized_rows=0,
        require_governance_checkpoint=True,
    )
    return plan_governed_candidates(
        profiles,
        policy,
        HierarchicalPlannerConfig(
            conservative_fallback_candidate_id=JOIN_THEN_AGGREGATE,
        ),
    )
