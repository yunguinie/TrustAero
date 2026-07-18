"""Fail-closed governance feasibility checks for physical-plan candidates.

This module deliberately does not estimate latency. It first decides which
physical candidates are allowed to enter a cost comparison. Keeping this step
separate prevents a fast but policy-incompatible candidate from winning a
ranking and then trying to justify its own governance compliance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

RAW_JOIN_LIMIT_EXCEEDED = "CANDIDATE_RAW_JOIN_LIMIT_EXCEEDED"
RAW_MATERIALIZATION_LIMIT_EXCEEDED = "CANDIDATE_RAW_MATERIALIZATION_LIMIT_EXCEEDED"


@dataclass(frozen=True)
class CandidateExposure:
    """Governance-relevant row exposure derived from a physical candidate.

    The optimizer must derive these counts from trusted plan construction or
    physical-plan inspection. They must not be accepted as an untrusted
    candidate's claim about itself.
    """

    candidate_id: str
    raw_rows_exposed_to_join: int
    raw_rows_materialized: int
    masked_rows_materialized: int = 0

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id cannot be empty")
        if (
            min(
                self.raw_rows_exposed_to_join,
                self.raw_rows_materialized,
                self.masked_rows_materialized,
            )
            < 0
        ):
            raise ValueError("Candidate exposure row counts cannot be negative")


@dataclass(frozen=True)
class GovernanceFeasibilityPolicy:
    """Small V1 policy fragment for raw-value exposure.

    A limit of ``None`` means unrestricted by this policy fragment. A limit of
    zero forbids the corresponding exposure. Positive limits support bounded
    exposure without silently turning a Boolean permission into a cost.
    """

    policy_id: str
    max_raw_join_rows: int | None
    max_raw_materialized_rows: int | None

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("policy_id cannot be empty")
        limits = (self.max_raw_join_rows, self.max_raw_materialized_rows)
        if any(value is not None and value < 0 for value in limits):
            raise ValueError("Governance exposure limits cannot be negative")


@dataclass(frozen=True)
class CandidateFeasibilityDiagnostic:
    """Stable, machine-readable explanation for one failed constraint."""

    code: str
    candidate_id: str
    exposure_kind: Literal["raw_join", "raw_materialization"]
    observed_rows: int
    allowed_rows: int


@dataclass(frozen=True)
class CandidateFeasibilityDecision:
    """Independent feasibility decision for one candidate."""

    candidate_id: str
    is_feasible: bool
    diagnostics: tuple[CandidateFeasibilityDiagnostic, ...]


@dataclass(frozen=True)
class CandidateFeasibilityResult:
    """Batch result consumed by a later cost-ranking stage."""

    policy_id: str
    status: Literal["ACCEPT", "REJECT"]
    decisions: tuple[CandidateFeasibilityDecision, ...]
    feasible_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]


def evaluate_candidate_feasibility(
    exposure: CandidateExposure,
    policy: GovernanceFeasibilityPolicy,
) -> CandidateFeasibilityDecision:
    """Evaluate hard constraints without reading or comparing candidate cost."""

    diagnostics: list[CandidateFeasibilityDiagnostic] = []
    if (
        policy.max_raw_join_rows is not None
        and exposure.raw_rows_exposed_to_join > policy.max_raw_join_rows
    ):
        diagnostics.append(
            CandidateFeasibilityDiagnostic(
                code=RAW_JOIN_LIMIT_EXCEEDED,
                candidate_id=exposure.candidate_id,
                exposure_kind="raw_join",
                observed_rows=exposure.raw_rows_exposed_to_join,
                allowed_rows=policy.max_raw_join_rows,
            )
        )
    if (
        policy.max_raw_materialized_rows is not None
        and exposure.raw_rows_materialized > policy.max_raw_materialized_rows
    ):
        diagnostics.append(
            CandidateFeasibilityDiagnostic(
                code=RAW_MATERIALIZATION_LIMIT_EXCEEDED,
                candidate_id=exposure.candidate_id,
                exposure_kind="raw_materialization",
                observed_rows=exposure.raw_rows_materialized,
                allowed_rows=policy.max_raw_materialized_rows,
            )
        )
    return CandidateFeasibilityDecision(
        candidate_id=exposure.candidate_id,
        is_feasible=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def filter_feasible_candidates(
    exposures: Sequence[CandidateExposure],
    policy: GovernanceFeasibilityPolicy,
) -> CandidateFeasibilityResult:
    """Filter a candidate set before cost ranking and reject an empty legal set.

    ``REJECT`` is a normal fail-closed result rather than an exception. Invalid
    API input, such as an empty candidate list or duplicate IDs, remains a
    programming error and raises ``ValueError``.
    """

    if not exposures:
        raise ValueError("At least one physical candidate is required")
    candidate_ids = [item.candidate_id for item in exposures]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Physical candidate IDs must be unique")

    decisions = tuple(evaluate_candidate_feasibility(item, policy) for item in exposures)
    feasible = tuple(item.candidate_id for item in decisions if item.is_feasible)
    rejected = tuple(item.candidate_id for item in decisions if not item.is_feasible)
    return CandidateFeasibilityResult(
        policy_id=policy.policy_id,
        status="ACCEPT" if feasible else "REJECT",
        decisions=decisions,
        feasible_candidate_ids=feasible,
        rejected_candidate_ids=rejected,
    )
