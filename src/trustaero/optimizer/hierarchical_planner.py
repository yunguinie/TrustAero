"""Fail-closed hierarchical planning for governed physical candidates.

The planner deliberately separates three questions that are easy to conflate:

1. Is a candidate legal under the active governance policy?
2. Is a legal candidate mechanically dominated by another equivalent plan?
3. Is there authorized performance evidence for choosing among the survivors?

This module answers the first two questions and provides an explicit
conservative fallback for the third.  It does *not* turn a development-only
threshold or a failed learned model into a production optimizer.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    CandidateFeasibilityResult,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)

NO_LEGAL_CANDIDATE = "HIERARCHICAL_NO_LEGAL_CANDIDATE"
ONLY_NONDOMINATED_CANDIDATE = "HIERARCHICAL_ONLY_NONDOMINATED_CANDIDATE"
CONSERVATIVE_FALLBACK = "HIERARCHICAL_CONSERVATIVE_FALLBACK"
AUTHORIZED_RANKER_REQUIRED = "HIERARCHICAL_AUTHORIZED_RANKER_REQUIRED"

PlannerStatus = Literal["SELECT", "REJECT", "DEFER"]


@dataclass(frozen=True, slots=True)
class GovernedCandidateProfile:
    """Trusted, comparable metadata for one physical candidate.

    ``work_metrics`` contains common nonnegative physical quantities such as
    hash input rows, checkpoint bytes, or lineage edges.  Metrics are used
    only for component-wise dominance, never as interchangeable cost units.
    Consequently, candidates with different metric-name sets are left
    incomparable rather than being assigned invented zero-cost components.
    """

    candidate_id: str
    result_equivalence_id: str
    exposure: CandidateExposure
    work_metrics: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.result_equivalence_id.strip():
            raise ValueError("Candidate and equivalence IDs cannot be empty")
        if self.exposure.candidate_id != self.candidate_id:
            raise ValueError("Candidate exposure must be bound to the same ID")
        names = [name for name, _value in self.work_metrics]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Dominance metrics must be sorted and unique")
        if any(not name.strip() for name in names):
            raise ValueError("Dominance metric names cannot be empty")
        if any(value < 0.0 or not math.isfinite(value) for _name, value in self.work_metrics):
            raise ValueError("Dominance metrics must be finite and nonnegative")

    def metrics(self) -> dict[str, float]:
        """Return the named physical quantities as a new mutable mapping."""

        return dict(self.work_metrics)


@dataclass(frozen=True, slots=True)
class CandidateDominance:
    """Auditable proof that one candidate is component-wise no worse."""

    dominator_candidate_id: str
    dominated_candidate_id: str
    equivalence_id: str
    strictly_better_dimensions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HierarchicalPlannerConfig:
    """Selection behavior after legality and dominance pruning.

    The fallback is a governance preference, not a latency prediction.  For
    example, a policy-first checkpoint can be preferred because it avoids raw
    materialization.  Setting it to ``None`` makes the planner return ``DEFER``
    whenever more than one non-dominated candidate survives.
    """

    conservative_fallback_candidate_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.conservative_fallback_candidate_id is not None
            and not self.conservative_fallback_candidate_id.strip()
        ):
            raise ValueError("Fallback candidate ID cannot be empty")


@dataclass(frozen=True, slots=True)
class HierarchicalPlanningResult:
    """Complete decision trace for one governed candidate set."""

    status: PlannerStatus
    selected_candidate_id: str | None
    reason_code: str
    feasible_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]
    nondominated_candidate_ids: tuple[str, ...]
    dominated_candidate_ids: tuple[str, ...]
    dominance_evidence: tuple[CandidateDominance, ...]
    feasibility: CandidateFeasibilityResult
    performance_model_used: bool = False


def hierarchical_planning_digest(result: HierarchicalPlanningResult) -> str:
    """Return a canonical digest for physical-plan/certificate binding.

    The complete decision trace is included: feasibility diagnostics,
    dominance evidence, selected candidate, reason code, and whether a
    performance model participated.  A certificate can bind this digest but
    cannot independently prove it; the verifier must receive a trusted
    recomputation when that stronger check is required.
    """

    payload = asdict(result)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _exposure_dimensions(exposure: CandidateExposure) -> dict[str, float]:
    """Expose common governance-risk quantities for dominance comparison."""

    return {
        "exposure.masked_rows_materialized": float(exposure.masked_rows_materialized),
        "exposure.raw_rows_exposed_to_join": float(exposure.raw_rows_exposed_to_join),
        "exposure.raw_rows_materialized": float(exposure.raw_rows_materialized),
    }


def _dominance_evidence(
    left: GovernedCandidateProfile,
    right: GovernedCandidateProfile,
) -> CandidateDominance | None:
    """Return proof when ``left`` safely dominates equivalent ``right``.

    Different result-equivalence classes or metric schemas are deliberately
    incomparable.  Every shared work and exposure dimension must be no worse,
    and at least one dimension must be strictly better.
    """

    if left.result_equivalence_id != right.result_equivalence_id:
        return None
    left_metrics = left.metrics()
    right_metrics = right.metrics()
    if left_metrics.keys() != right_metrics.keys():
        return None
    left_dimensions = {**left_metrics, **_exposure_dimensions(left.exposure)}
    right_dimensions = {**right_metrics, **_exposure_dimensions(right.exposure)}
    if any(left_dimensions[name] > right_dimensions[name] for name in left_dimensions):
        return None
    strict = tuple(
        sorted(name for name in left_dimensions if left_dimensions[name] < right_dimensions[name])
    )
    if not strict:
        return None
    return CandidateDominance(
        dominator_candidate_id=left.candidate_id,
        dominated_candidate_id=right.candidate_id,
        equivalence_id=left.result_equivalence_id,
        strictly_better_dimensions=strict,
    )


def prune_dominated_candidates(
    profiles: Sequence[GovernedCandidateProfile],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[CandidateDominance, ...],
]:
    """Remove candidates with a direct component-wise dominance proof."""

    candidate_ids = [profile.candidate_id for profile in profiles]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Governed candidate IDs must be unique")

    evidence: list[CandidateDominance] = []
    dominated: set[str] = set()
    for right in profiles:
        proofs = tuple(
            proof
            for left in profiles
            if left.candidate_id != right.candidate_id
            and (proof := _dominance_evidence(left, right)) is not None
        )
        if proofs:
            # Keep every direct proof for auditability; the deterministic ID
            # order makes serialized diagnostics reproducible.
            dominated.add(right.candidate_id)
            evidence.extend(proofs)
    nondominated = tuple(
        profile.candidate_id for profile in profiles if profile.candidate_id not in dominated
    )
    return (
        nondominated,
        tuple(sorted(dominated)),
        tuple(
            sorted(
                evidence,
                key=lambda item: (
                    item.dominated_candidate_id,
                    item.dominator_candidate_id,
                ),
            )
        ),
    )


def plan_governed_candidates(
    profiles: Sequence[GovernedCandidateProfile],
    policy: GovernanceFeasibilityPolicy,
    config: HierarchicalPlannerConfig | None = None,
) -> HierarchicalPlanningResult:
    """Filter legality, prune dominance, then select or explicitly defer."""

    if not profiles:
        raise ValueError("At least one governed candidate profile is required")
    config = config or HierarchicalPlannerConfig()
    candidate_ids = [profile.candidate_id for profile in profiles]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Governed candidate IDs must be unique")

    feasibility = filter_feasible_candidates(
        tuple(profile.exposure for profile in profiles),
        policy,
    )
    if feasibility.status == "REJECT":
        return HierarchicalPlanningResult(
            status="REJECT",
            selected_candidate_id=None,
            reason_code=NO_LEGAL_CANDIDATE,
            feasible_candidate_ids=(),
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            nondominated_candidate_ids=(),
            dominated_candidate_ids=(),
            dominance_evidence=(),
            feasibility=feasibility,
        )

    feasible_profiles = tuple(
        profile
        for profile in profiles
        if profile.candidate_id in feasibility.feasible_candidate_ids
    )
    nondominated, dominated, evidence = prune_dominated_candidates(feasible_profiles)
    if len(nondominated) == 1:
        return HierarchicalPlanningResult(
            status="SELECT",
            selected_candidate_id=nondominated[0],
            reason_code=ONLY_NONDOMINATED_CANDIDATE,
            feasible_candidate_ids=feasibility.feasible_candidate_ids,
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            nondominated_candidate_ids=nondominated,
            dominated_candidate_ids=dominated,
            dominance_evidence=evidence,
            feasibility=feasibility,
        )

    fallback = config.conservative_fallback_candidate_id
    if fallback is not None and fallback in nondominated:
        return HierarchicalPlanningResult(
            status="SELECT",
            selected_candidate_id=fallback,
            reason_code=CONSERVATIVE_FALLBACK,
            feasible_candidate_ids=feasibility.feasible_candidate_ids,
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            nondominated_candidate_ids=nondominated,
            dominated_candidate_ids=dominated,
            dominance_evidence=evidence,
            feasibility=feasibility,
        )
    return HierarchicalPlanningResult(
        status="DEFER",
        selected_candidate_id=None,
        reason_code=AUTHORIZED_RANKER_REQUIRED,
        feasible_candidate_ids=feasibility.feasible_candidate_ids,
        rejected_candidate_ids=feasibility.rejected_candidate_ids,
        nondominated_candidate_ids=nondominated,
        dominated_candidate_ids=dominated,
        dominance_evidence=evidence,
        feasibility=feasibility,
    )
