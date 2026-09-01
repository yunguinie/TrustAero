"""Governance-safe runtime calibration for checkpoint placement.

The static checkpoint models are useful when their physical assumptions match
the current source.  Their two independent real-month failures also show why a
deployment cannot silently trust a small estimated cost gap.  This module
therefore supports a bounded runtime calibration mode:

1. governance feasibility removes illegal candidates;
2. only the remaining candidates may be piloted;
3. balanced paired timings are summarized by a deterministic bootstrap;
4. a material winner is selected, otherwise a frozen legal baseline is used.

The pilot is an execution cost and must be reported or amortized through a
snapshot-bound cache.  It is never treated as free optimizer metadata.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Literal

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)

PilotConclusion = Literal[
    "POLICY_FIRST_MATERIALLY_FASTER",
    "QUERY_FIRST_MATERIALLY_FASTER",
    "INCONCLUSIVE",
    "NOT_REQUIRED",
]


@dataclass(frozen=True, slots=True)
class PilotLatencyBlock:
    """One balanced block containing both legal candidate latencies."""

    block_id: int
    policy_first_ms: float
    query_first_ms: float

    def __post_init__(self) -> None:
        values = (self.policy_first_ms, self.query_first_ms)
        if self.block_id < 0 or any(value <= 0.0 or not math.isfinite(value) for value in values):
            raise ValueError("Adaptive checkpoint pilot latency block is invalid")


@dataclass(frozen=True, slots=True)
class AdaptiveCheckpointConfig:
    """Pre-registered statistical and fallback controls."""

    practical_tie_fraction: float = 0.03
    confidence_level: float = 0.95
    bootstrap_draws: int = 2_000
    bootstrap_seed: int = 20260723
    minimum_paired_blocks: int = 8
    fallback_query_selectivity_threshold: float = 0.35

    def __post_init__(self) -> None:
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("Adaptive checkpoint practical-tie fraction is invalid")
        if not 0.5 < self.confidence_level < 1.0:
            raise ValueError("Adaptive checkpoint confidence level is invalid")
        if self.bootstrap_draws < 500:
            raise ValueError("Adaptive checkpoint bootstrap is too small")
        if self.minimum_paired_blocks < 4:
            raise ValueError("Adaptive checkpoint pilot is too small")
        if not 0.0 <= self.fallback_query_selectivity_threshold <= 1.0:
            raise ValueError("Adaptive checkpoint fallback threshold is invalid")


@dataclass(frozen=True, slots=True)
class AdaptiveCheckpointDecision:
    """Auditable result of feasibility, pilot inference, and fallback."""

    status: Literal["SELECT", "REJECT"]
    selected_candidate_id: str | None
    reason_code: str
    feasible_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]
    pilot_conclusion: PilotConclusion
    policy_first_over_query_first_ratio: float | None
    confidence_interval: tuple[float, float] | None
    paired_block_count: int
    pilot_cost_ms: float
    governance_before_pilot: bool = True


def _bootstrap_median_ratio_interval(
    blocks: tuple[PilotLatencyBlock, ...],
    *,
    confidence_level: float,
    draws: int,
    seed: int,
) -> tuple[float, float, float]:
    """Return median ratio and a deterministic paired bootstrap interval."""

    log_ratios = tuple(math.log(block.policy_first_ms / block.query_first_ms) for block in blocks)
    point = math.exp(statistics.median(log_ratios))
    generator = random.Random(seed)
    bootstrapped: list[float] = []
    for _draw in range(draws):
        sample = [log_ratios[generator.randrange(len(log_ratios))] for _ in log_ratios]
        bootstrapped.append(math.exp(statistics.median(sample)))
    bootstrapped.sort()
    alpha = (1.0 - confidence_level) / 2.0
    lower_index = max(0, math.floor(alpha * (draws - 1)))
    upper_index = min(draws - 1, math.ceil((1.0 - alpha) * (draws - 1)))
    return point, bootstrapped[lower_index], bootstrapped[upper_index]


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


def choose_adaptive_checkpoint(
    statistics: GovernedCheckpointStatistics,
    policy: GovernanceFeasibilityPolicy,
    pilot_blocks: tuple[PilotLatencyBlock, ...],
    config: AdaptiveCheckpointConfig,
) -> AdaptiveCheckpointDecision:
    """Select a checkpoint plan without ever piloting an illegal candidate."""

    feasibility = filter_feasible_candidates(_candidate_exposures(statistics), policy)
    if feasibility.status == "REJECT":
        return AdaptiveCheckpointDecision(
            status="REJECT",
            selected_candidate_id=None,
            reason_code="ADAPTIVE_CHECKPOINT_NO_LEGAL_CANDIDATE",
            feasible_candidate_ids=(),
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            pilot_conclusion="NOT_REQUIRED",
            policy_first_over_query_first_ratio=None,
            confidence_interval=None,
            paired_block_count=0,
            pilot_cost_ms=0.0,
        )
    if len(feasibility.feasible_candidate_ids) == 1:
        # A paired block necessarily executed both candidates.  Receiving one
        # under a single-candidate policy proves the caller violated feasibility.
        if pilot_blocks:
            raise ValueError("Pilot observations contain a governance-illegal candidate")
        selected = feasibility.feasible_candidate_ids[0]
        return AdaptiveCheckpointDecision(
            status="SELECT",
            selected_candidate_id=selected,
            reason_code="ADAPTIVE_CHECKPOINT_ONLY_LEGAL_CANDIDATE",
            feasible_candidate_ids=feasibility.feasible_candidate_ids,
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            pilot_conclusion="NOT_REQUIRED",
            policy_first_over_query_first_ratio=None,
            confidence_interval=None,
            paired_block_count=0,
            pilot_cost_ms=0.0,
        )
    if len(pilot_blocks) < config.minimum_paired_blocks:
        raise ValueError("Adaptive checkpoint pilot has insufficient paired blocks")
    if len({block.block_id for block in pilot_blocks}) != len(pilot_blocks):
        raise ValueError("Adaptive checkpoint pilot block IDs are not unique")

    point, lower, upper = _bootstrap_median_ratio_interval(
        pilot_blocks,
        confidence_level=config.confidence_level,
        draws=config.bootstrap_draws,
        seed=config.bootstrap_seed,
    )
    lower_material = 1.0 / (1.0 + config.practical_tie_fraction)
    upper_material = 1.0 + config.practical_tie_fraction
    if upper < lower_material:
        selected = POLICY_FIRST_CHECKPOINT
        conclusion: PilotConclusion = "POLICY_FIRST_MATERIALLY_FASTER"
        reason = "ADAPTIVE_CHECKPOINT_PILOT_POLICY_FIRST"
    elif lower > upper_material:
        selected = QUERY_FIRST_CHECKPOINT
        conclusion = "QUERY_FIRST_MATERIALLY_FASTER"
        reason = "ADAPTIVE_CHECKPOINT_PILOT_QUERY_FIRST"
    else:
        query_rate = statistics.estimated_query_rows / statistics.input_rows
        selected = (
            QUERY_FIRST_CHECKPOINT
            if query_rate < config.fallback_query_selectivity_threshold
            else POLICY_FIRST_CHECKPOINT
        )
        conclusion = "INCONCLUSIVE"
        reason = "ADAPTIVE_CHECKPOINT_INCONCLUSIVE_FROZEN_BASELINE"
    if selected not in feasibility.feasible_candidate_ids:
        raise AssertionError("Adaptive checkpoint selected an illegal candidate")
    return AdaptiveCheckpointDecision(
        status="SELECT",
        selected_candidate_id=selected,
        reason_code=reason,
        feasible_candidate_ids=feasibility.feasible_candidate_ids,
        rejected_candidate_ids=feasibility.rejected_candidate_ids,
        pilot_conclusion=conclusion,
        policy_first_over_query_first_ratio=point,
        confidence_interval=(lower, upper),
        paired_block_count=len(pilot_blocks),
        pilot_cost_ms=sum(block.policy_first_ms + block.query_first_ms for block in pilot_blocks),
    )
