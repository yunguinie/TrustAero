"""Execution-aware selection for a required pre-Join governance checkpoint.

The optimizer never learns a direct winner label. Trusted planner statistics
are translated into physical work, governance feasibility removes illegal raw
materialization first, and an analytic model prices the remaining work. The
policy-first plan is the conservative fallback for ties or unsupported input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.optimizer.execution_aware import (
    AnalyticExecutionCostModel,
    CandidateCostEstimate,
    ExecutionAwareRankingResult,
    ExecutionAwareWorkVector,
    estimate_execution_cost,
)

PracticalTieStrategy = Literal[
    "policy_first_fallback",
    "minimum_analytic_cost",
]
POLICY_FIRST_CHECKPOINT = "policy_first_narrow_checkpoint"
QUERY_FIRST_CHECKPOINT = "query_first_raw_checkpoint"
GIB = float(1024**3)
MILLION = 1_000_000.0

StatisticProvenance = Literal[
    "planner_derived",
    "catalog_exact_controlled",
    "catalog_estimate",
]


@dataclass(frozen=True, slots=True)
class GovernedCheckpointStatistics:
    """Trusted cardinality and width estimates available before execution."""

    input_rows: int
    sensitive_width_bytes: float
    estimated_policy_rows: int
    estimated_query_rows: int
    estimated_result_rows: int
    statistic_provenance: StatisticProvenance

    def __post_init__(self) -> None:
        counts = (
            self.input_rows,
            self.estimated_policy_rows,
            self.estimated_query_rows,
            self.estimated_result_rows,
        )
        if self.input_rows <= 0 or any(value < 0 for value in counts):
            raise ValueError("Governed-checkpoint row counts are invalid")
        if max(counts[1:]) > self.input_rows:
            raise ValueError("Governed-checkpoint estimates exceed input rows")
        if self.estimated_result_rows > min(self.estimated_policy_rows, self.estimated_query_rows):
            raise ValueError("Result estimate exceeds a filtering input")
        if self.sensitive_width_bytes <= 0.0 or not math.isfinite(self.sensitive_width_bytes):
            raise ValueError("Sensitive width must be finite and positive")


def derive_governed_checkpoint_work(
    statistics: GovernedCheckpointStatistics,
    candidate_id: str,
) -> ExecutionAwareWorkVector:
    """Translate one trusted candidate into additive physical work units."""

    if candidate_id == POLICY_FIRST_CHECKPOINT:
        policy_hash_rows = statistics.input_rows
        checkpoint_rows = statistics.estimated_policy_rows
        narrow_bytes = checkpoint_rows * 24.0
        raw_bytes = 0.0
    elif candidate_id == QUERY_FIRST_CHECKPOINT:
        policy_hash_rows = statistics.estimated_query_rows
        checkpoint_rows = statistics.estimated_query_rows
        narrow_bytes = 0.0
        raw_bytes = checkpoint_rows * (16.0 + statistics.sensitive_width_bytes)
    else:
        raise ValueError(f"Unknown governed-checkpoint candidate: {candidate_id}")
    features = {
        "checkpoint.narrow_write_gib": narrow_bytes / GIB,
        "checkpoint.post_rows_million": checkpoint_rows / MILLION,
        "checkpoint.raw_write_gib": raw_bytes / GIB,
        "join.result_rows_million": statistics.estimated_result_rows / MILLION,
        "policy_hash.input_gib": (policy_hash_rows * statistics.sensitive_width_bytes / GIB),
    }
    return ExecutionAwareWorkVector(
        candidate_id=candidate_id,
        physical_plan_id=f"governed-checkpoint:{candidate_id}",
        statistic_provenance=statistics.statistic_provenance,
        features=tuple(sorted(features.items())),
    )


def _candidate_exposures(
    statistics: GovernedCheckpointStatistics,
) -> tuple[CandidateExposure, ...]:
    return (
        CandidateExposure(POLICY_FIRST_CHECKPOINT, 0, 0),
        CandidateExposure(
            QUERY_FIRST_CHECKPOINT,
            0,
            statistics.estimated_query_rows,
        ),
    )


def rank_governed_checkpoint_candidates(
    statistics: GovernedCheckpointStatistics,
    policy: GovernanceFeasibilityPolicy,
    model: AnalyticExecutionCostModel,
    *,
    practical_tie_strategy: PracticalTieStrategy = "policy_first_fallback",
) -> ExecutionAwareRankingResult:
    """Filter by governance, then rank legal checkpoint placements by cost.

    ``policy_first_fallback`` preserves the frozen V4 behavior.  V4.1 uses
    ``minimum_analytic_cost`` after both candidates have passed governance:
    a small estimated performance gap is not itself a safety reason to
    override the cheaper legal plan.  Out-of-support inputs still fail back
    to the policy-first plan.
    """

    if practical_tie_strategy not in ("policy_first_fallback", "minimum_analytic_cost"):
        raise ValueError(f"Unknown practical-tie strategy: {practical_tie_strategy}")

    exposures = _candidate_exposures(statistics)
    feasibility = filter_feasible_candidates(exposures, policy)
    if feasibility.status == "REJECT":
        return ExecutionAwareRankingResult(
            status="REJECT",
            selected_candidate_id=None,
            reason_code="GOVERNED_CHECKPOINT_NO_LEGAL_CANDIDATE",
            feasible_candidate_ids=(),
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            practically_tied_candidate_ids=(),
            estimates=(),
            feasibility=feasibility,
        )
    if len(feasibility.feasible_candidate_ids) == 1:
        selected = feasibility.feasible_candidate_ids[0]
        return ExecutionAwareRankingResult(
            status="SELECT",
            selected_candidate_id=selected,
            reason_code="GOVERNED_CHECKPOINT_ONLY_LEGAL_CANDIDATE",
            feasible_candidate_ids=feasibility.feasible_candidate_ids,
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            practically_tied_candidate_ids=(selected,),
            estimates=(),
            feasibility=feasibility,
        )

    estimates = tuple(
        estimate_execution_cost(derive_governed_checkpoint_work(statistics, candidate_id), model)
        for candidate_id in feasibility.feasible_candidate_ids
    )
    if not all(estimate.within_calibration_support for estimate in estimates):
        return ExecutionAwareRankingResult(
            status="SELECT",
            selected_candidate_id=POLICY_FIRST_CHECKPOINT,
            reason_code="GOVERNED_CHECKPOINT_OUT_OF_SUPPORT_SAFE_FALLBACK",
            feasible_candidate_ids=feasibility.feasible_candidate_ids,
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            practically_tied_candidate_ids=(),
            estimates=estimates,
            feasibility=feasibility,
        )
    best = min(estimate.total_ms for estimate in estimates)
    tied = tuple(
        sorted(
            estimate.candidate_id
            for estimate in estimates
            if estimate.total_ms <= best * (1.0 + model.practical_tie_fraction)
        )
    )
    if len(tied) > 1:
        if practical_tie_strategy == "minimum_analytic_cost":
            # Governance filtering has already removed every illegal candidate.
            # Keep the practical-tie set for diagnostics, but do not replace the
            # analytic winner with a performance-unrelated fixed preference.
            selected = min(
                estimates,
                key=lambda estimate: (estimate.total_ms, estimate.candidate_id),
            ).candidate_id
            reason = "GOVERNED_CHECKPOINT_PRACTICAL_TIE_MINIMUM_ANALYTIC_COST"
        else:
            selected = POLICY_FIRST_CHECKPOINT if POLICY_FIRST_CHECKPOINT in tied else tied[0]
            reason = "GOVERNED_CHECKPOINT_PRACTICAL_TIE_SAFE_FALLBACK"
    else:
        selected = tied[0]
        reason = "GOVERNED_CHECKPOINT_MINIMUM_ANALYTIC_COST"
    return ExecutionAwareRankingResult(
        status="SELECT",
        selected_candidate_id=selected,
        reason_code=reason,
        feasible_candidate_ids=feasibility.feasible_candidate_ids,
        rejected_candidate_ids=feasibility.rejected_candidate_ids,
        practically_tied_candidate_ids=tied,
        estimates=tuple(
            CandidateCostEstimate(
                candidate_id=estimate.candidate_id,
                total_ms=estimate.total_ms,
                component_costs_ms=estimate.component_costs_ms,
                within_calibration_support=estimate.within_calibration_support,
            )
            for estimate in estimates
        ),
        feasibility=feasibility,
    )
