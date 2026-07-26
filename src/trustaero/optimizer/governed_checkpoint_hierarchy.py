"""Hierarchical planning adapter for governed-checkpoint candidates.

This adapter connects the generic legality/dominance planner to the three
checkpoint execution shapes used by TrustAero.  Its fallback choices express
governance conservatism only:

* without a required checkpoint, prefer the fused plan that exposes and
  materializes no raw sensitive rows;
* with a required checkpoint, prefer policy-first, whose checkpoint contains
  no raw sensitive value.

No development threshold or failed learned model participates in this path.
"""

from __future__ import annotations

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)
from trustaero.optimizer.hierarchical_planner import (
    GovernedCandidateProfile,
    HierarchicalPlannerConfig,
    HierarchicalPlanningResult,
    plan_governed_candidates,
)

FUSED_GOVERNED_PIPELINE = "fused_governed_pipeline"
CHECKPOINT_RESULT_EQUIVALENCE = "governed-checkpoint-result-v1"


def derive_checkpoint_hierarchy_profiles(
    statistics: GovernedCheckpointStatistics,
) -> tuple[GovernedCandidateProfile, ...]:
    """Derive common physical quantities for the three candidate shapes."""

    input_rows = float(statistics.input_rows)
    policy_rows = float(statistics.estimated_policy_rows)
    query_rows = float(statistics.estimated_query_rows)
    width = statistics.sensitive_width_bytes

    # Metrics share names and units so component-wise dominance is meaningful.
    # They are physical work quantities, not latency predictions.
    return (
        GovernedCandidateProfile(
            candidate_id=FUSED_GOVERNED_PIPELINE,
            result_equivalence_id=CHECKPOINT_RESULT_EQUIVALENCE,
            exposure=CandidateExposure(
                FUSED_GOVERNED_PIPELINE,
                raw_rows_exposed_to_join=0,
                raw_rows_materialized=0,
                provides_governance_checkpoint=False,
            ),
            work_metrics=(
                ("checkpoint.payload_bytes", 0.0),
                ("checkpoint.rows", 0.0),
                ("pipeline_breaker.count", 0.0),
                ("policy_hash.input_bytes", input_rows * width),
                ("policy_hash.rows", input_rows),
            ),
        ),
        GovernedCandidateProfile(
            candidate_id=POLICY_FIRST_CHECKPOINT,
            result_equivalence_id=CHECKPOINT_RESULT_EQUIVALENCE,
            exposure=CandidateExposure(
                POLICY_FIRST_CHECKPOINT,
                raw_rows_exposed_to_join=0,
                raw_rows_materialized=0,
                provides_governance_checkpoint=True,
            ),
            work_metrics=(
                # The narrow checkpoint contains row_id, join_key, and the
                # query bucket; 24 bytes matches the frozen EA representation.
                ("checkpoint.payload_bytes", policy_rows * 24.0),
                ("checkpoint.rows", policy_rows),
                ("pipeline_breaker.count", 1.0),
                ("policy_hash.input_bytes", input_rows * width),
                ("policy_hash.rows", input_rows),
            ),
        ),
        GovernedCandidateProfile(
            candidate_id=QUERY_FIRST_CHECKPOINT,
            result_equivalence_id=CHECKPOINT_RESULT_EQUIVALENCE,
            exposure=CandidateExposure(
                QUERY_FIRST_CHECKPOINT,
                raw_rows_exposed_to_join=0,
                raw_rows_materialized=statistics.estimated_query_rows,
                provides_governance_checkpoint=True,
            ),
            work_metrics=(
                # This checkpoint retains row_id, join_key, and the raw
                # sensitive payload so the policy can be evaluated later.
                (
                    "checkpoint.payload_bytes",
                    query_rows * (16.0 + width),
                ),
                ("checkpoint.rows", query_rows),
                ("pipeline_breaker.count", 1.0),
                ("policy_hash.input_bytes", query_rows * width),
                ("policy_hash.rows", query_rows),
            ),
        ),
    )


def plan_checkpoint_hierarchically(
    statistics: GovernedCheckpointStatistics,
    policy: GovernanceFeasibilityPolicy,
) -> HierarchicalPlanningResult:
    """Plan checkpoints without claiming an unauthorized cost-model win."""

    fallback = (
        POLICY_FIRST_CHECKPOINT if policy.require_governance_checkpoint else FUSED_GOVERNED_PIPELINE
    )
    return plan_governed_candidates(
        derive_checkpoint_hierarchy_profiles(statistics),
        policy,
        HierarchicalPlannerConfig(
            conservative_fallback_candidate_id=fallback,
        ),
    )
