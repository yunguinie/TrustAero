"""One-sided uncertainty protection for governed-checkpoint selection.

The analytic optimizer predicts the latency margin ``query - policy``.  Query
first is selected only when its predicted advantage exceeds a grouped error
bound calibrated without the failed holdout.  Governance feasibility and
out-of-support handling remain hard constraints in the base optimizer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.execution_aware import (
    AnalyticExecutionCostModel,
    ExecutionAwareRankingResult,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
    rank_governed_checkpoint_candidates,
)

QUERY_CONFIDENT = "GOVERNED_CHECKPOINT_QUERY_ADVANTAGE_EXCEEDS_ERROR_BOUND"
UNCERTAIN_FALLBACK = "GOVERNED_CHECKPOINT_UNCERTAINTY_SAFE_FALLBACK"


@dataclass(frozen=True, slots=True)
class CheckpointUncertaintyGuard:
    """Frozen one-sided error bound wrapped around an analytic base model."""

    base_model: AnalyticExecutionCostModel
    query_margin_error_upper_ms: float
    coverage: float
    calibration_family_count: int
    calibration_method: str = "action_conditional_grouped_conformal_v1"

    def __post_init__(self) -> None:
        if self.query_margin_error_upper_ms < 0.0 or not math.isfinite(
            self.query_margin_error_upper_ms
        ):
            raise ValueError("Checkpoint uncertainty bound must be nonnegative")
        if not 0.0 < self.coverage < 1.0:
            raise ValueError("Checkpoint uncertainty coverage must be in (0, 1)")
        if self.calibration_family_count < 2:
            raise ValueError("Checkpoint uncertainty requires multiple families")
        if self.calibration_method != "action_conditional_grouped_conformal_v1":
            raise ValueError("Unknown checkpoint uncertainty calibration method")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "uncertainty_aware_governed_checkpoint_v1",
            "base_model": self.base_model.to_dict(),
            "query_margin_error_upper_ms": self.query_margin_error_upper_ms,
            "coverage": self.coverage,
            "calibration_family_count": self.calibration_family_count,
            "calibration_method": self.calibration_method,
            "governance_before_cost": True,
            "safe_fallback": POLICY_FIRST_CHECKPOINT,
        }


def rank_uncertainty_aware_checkpoint_candidates(
    statistics: GovernedCheckpointStatistics,
    policy: GovernanceFeasibilityPolicy,
    guard: CheckpointUncertaintyGuard,
) -> ExecutionAwareRankingResult:
    """Apply legality/support checks before the one-sided cost decision."""

    base = rank_governed_checkpoint_candidates(statistics, policy, guard.base_model)
    # Rejections, a single legal plan, and OOD fallback are already fail-closed.
    if base.status == "REJECT" or len(base.feasible_candidate_ids) <= 1:
        return base
    if base.reason_code == "GOVERNED_CHECKPOINT_OUT_OF_SUPPORT_SAFE_FALLBACK":
        return base
    estimates = {item.candidate_id: item.total_ms for item in base.estimates}
    if set(estimates) != {POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT}:
        raise ValueError("Uncertainty guard requires both checkpoint cost estimates")
    predicted_margin_ms = estimates[QUERY_FIRST_CHECKPOINT] - estimates[POLICY_FIRST_CHECKPOINT]
    # An upper bound below zero means query-first remains faster even after the
    # calibrated adverse prediction error is added.
    query_upper_margin_ms = predicted_margin_ms + guard.query_margin_error_upper_ms
    if query_upper_margin_ms < 0.0:
        selected = QUERY_FIRST_CHECKPOINT
        reason = QUERY_CONFIDENT
        tied: tuple[str, ...] = (QUERY_FIRST_CHECKPOINT,)
    else:
        selected = POLICY_FIRST_CHECKPOINT
        reason = UNCERTAIN_FALLBACK
        tied = (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
    return ExecutionAwareRankingResult(
        status="SELECT",
        selected_candidate_id=selected,
        reason_code=reason,
        feasible_candidate_ids=base.feasible_candidate_ids,
        rejected_candidate_ids=base.rejected_candidate_ids,
        practically_tied_candidate_ids=tied,
        estimates=base.estimates,
        feasibility=base.feasibility,
    )
