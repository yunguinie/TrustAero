"""Explainable V1 selector for placing a required hash Mask around a Join.

This module selects only between candidates that the physical-plan generator
has already proved legal.  It does not make Mask freely commutative: the
caller must report candidate legality after the normal fail-closed checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MaskPlacement(StrEnum):
    """The two bounded choices supported by the Phase 2E fragment."""

    EARLY = "early_mask"
    LATE = "late_mask"


@dataclass(frozen=True)
class MaskPlacementFeatures:
    """Statistics available before executing either legal candidate.

    ``join_input_rows`` is the estimated fact-side cardinality entering the
    Join. ``join_match_rate`` estimates which fraction survives that Join.
    Candidate legality comes from semantic validation, not from this model.
    """

    join_input_rows: int
    identifier_width_bytes: int
    join_match_rate: float
    early_mask_legal: bool = True
    late_mask_legal: bool = True
    max_raw_exposure_rows: int | None = None

    def __post_init__(self) -> None:
        if self.join_input_rows < 0:
            raise ValueError("join_input_rows must be non-negative")
        if self.identifier_width_bytes <= 0:
            raise ValueError("identifier_width_bytes must be positive")
        if not 0.0 <= self.join_match_rate <= 1.0:
            raise ValueError("join_match_rate must be between zero and one")
        if self.max_raw_exposure_rows is not None and self.max_raw_exposure_rows < 0:
            raise ValueError("max_raw_exposure_rows must be non-negative when set")


@dataclass(frozen=True)
class MaskOptimizerConfig:
    """Frozen constants for the deliberately small V1 proxy cost model.

    The setup term is calibrated from Phase 2E confirmation and represents the
    fixed cost of materializing the early-Mask boundary. Proxy values are work
    units measured in bytes, not predictions of milliseconds.
    """

    hashed_identifier_width_bytes: int = 64
    early_materialization_setup_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.hashed_identifier_width_bytes <= 0:
            raise ValueError("hashed_identifier_width_bytes must be positive")
        if self.early_materialization_setup_bytes < 0:
            raise ValueError("early_materialization_setup_bytes must be non-negative")


@dataclass(frozen=True)
class MaskPlacementDecision:
    """Auditable optimizer output, including feasibility and proxy costs."""

    placement: MaskPlacement
    reason_code: str
    early_proxy_work_bytes: float
    late_proxy_work_bytes: float
    estimated_join_output_rows: float
    estimated_early_raw_exposure_rows: int
    estimated_late_raw_exposure_rows: int


def choose_mask_placement(
    features: MaskPlacementFeatures,
    config: MaskOptimizerConfig | None = None,
) -> MaskPlacementDecision:
    """Choose the lowest-proxy-cost feasible placement.

    V1 uses a transparent model:

    * early: hash every input identifier, Join its 64-byte hash, and pay a
      fixed materialization setup term;
    * late: carry raw identifiers through the Join, then hash matched rows.

    A governance exposure limit is a hard feasibility constraint and is never
    traded against runtime cost.
    """

    model = config or MaskOptimizerConfig()
    output_rows = features.join_input_rows * features.join_match_rate
    early_work = (
        features.join_input_rows * features.identifier_width_bytes
        + features.join_input_rows * model.hashed_identifier_width_bytes
        + model.early_materialization_setup_bytes
    )
    late_work = (
        features.join_input_rows * features.identifier_width_bytes
        + output_rows * features.identifier_width_bytes
    )

    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")

    if early_feasible and not late_feasible:
        placement = MaskPlacement.EARLY
        reason = "MASK_OPTIMIZER_LATE_INFEASIBLE"
    elif late_feasible and not early_feasible:
        placement = MaskPlacement.LATE
        reason = "MASK_OPTIMIZER_EARLY_INFEASIBLE"
    elif early_work < late_work:
        placement = MaskPlacement.EARLY
        reason = "MASK_OPTIMIZER_EARLY_LOWER_PROXY_COST"
    else:
        # A deterministic late tie-break avoids an unnecessary materialization.
        placement = MaskPlacement.LATE
        reason = "MASK_OPTIMIZER_LATE_LOWER_OR_EQUAL_PROXY_COST"

    return MaskPlacementDecision(
        placement=placement,
        reason_code=reason,
        early_proxy_work_bytes=float(early_work),
        late_proxy_work_bytes=float(late_work),
        estimated_join_output_rows=float(output_rows),
        estimated_early_raw_exposure_rows=0,
        estimated_late_raw_exposure_rows=features.join_input_rows,
    )
