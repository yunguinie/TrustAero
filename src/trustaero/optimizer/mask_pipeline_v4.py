"""Candidate-level physical-work contract for a future Mask Optimizer V4.

This module deliberately contains no fitted model.  It freezes which
pre-execution statistics a real-pipeline optimizer may consume and translates
them into auditable work quantities for each already-legal candidate.  Model
selection is deferred until January development measurements validate the
contract; February--December data must not influence it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal

from trustaero.optimizer.mask import MaskPlacement

V4_WORK_FEATURE_NAMES = (
    "log_source_scan_payload_mib",
    "log_sensitive_derivation_payload_mib",
    "log_pre_join_hash_payload_mib",
    "log_post_join_hash_payload_mib",
    "log_join_fact_payload_mib",
    "log_dimension_build_payload_mib",
    "log_join_output_rows_100k",
    "log_boundary_payload_mib",
    "log_output_payload_mib",
    "log_sort_key_comparison_mib",
    "pipeline_breaker",
)


@dataclass(frozen=True, slots=True)
class RealPipelineWorkloadStats:
    """Pre-execution statistics for the bounded DuckDB Mask/Join fragment.

    Row counts are estimates available before candidate execution.  During
    development they may come from exact controlled catalog statistics, but
    the provenance remains explicit so a future holdout cannot use observed
    candidate runtimes or post-execution labels as optimizer inputs.

    Widths are logical payload-byte estimates, not Python object sizes or
    DuckDB allocation measurements.  They must be derived identically in
    development and external evaluation.
    """

    source_scan_rows: int
    join_input_rows: int
    join_output_rows_estimate: int
    dimension_build_rows: int
    sensitive_raw_width_bytes: float
    source_scan_payload_width_bytes: float
    join_fact_fixed_width_bytes: float
    dimension_build_payload_width_bytes: float
    dimension_output_payload_width_bytes: float
    output_fixed_width_bytes: float
    sort_key_width_bytes: float
    statistic_provenance: Literal[
        "catalog_exact_controlled", "catalog_estimate", "development_observation"
    ]
    early_mask_legal: bool = True
    late_mask_legal: bool = True
    max_raw_exposure_rows: int | None = None
    masked_width_bytes: int = 64

    def __post_init__(self) -> None:
        if self.source_scan_rows < self.join_input_rows or self.join_input_rows < 0:
            raise ValueError("source_scan_rows must cover the nonnegative Join input")
        if not 0 <= self.join_output_rows_estimate <= self.join_input_rows:
            raise ValueError("V4 currently supports a filtering many-to-one inner Join")
        if self.dimension_build_rows < 0:
            raise ValueError("dimension_build_rows must be nonnegative")
        widths = (
            self.sensitive_raw_width_bytes,
            self.source_scan_payload_width_bytes,
            self.join_fact_fixed_width_bytes,
            self.dimension_build_payload_width_bytes,
            self.dimension_output_payload_width_bytes,
            self.output_fixed_width_bytes,
            self.sort_key_width_bytes,
        )
        if any(width < 0.0 or not math.isfinite(width) for width in widths):
            raise ValueError("V4 logical payload widths must be finite and nonnegative")
        if self.sensitive_raw_width_bytes <= 0.0 or self.masked_width_bytes <= 0:
            raise ValueError("Sensitive raw and masked widths must be positive")
        if self.source_scan_rows > 0 and self.source_scan_payload_width_bytes <= 0.0:
            raise ValueError("A nonempty source must have a positive scan payload width")
        if self.join_output_rows_estimate > 0 and self.dimension_build_rows == 0:
            raise ValueError("A nonempty many-to-one Join output requires build rows")
        if self.max_raw_exposure_rows is not None and self.max_raw_exposure_rows < 0:
            raise ValueError("Raw exposure limit must be nonnegative")

    @property
    def join_match_rate(self) -> float:
        if self.join_input_rows == 0:
            return 0.0
        return self.join_output_rows_estimate / self.join_input_rows

    def placement_is_legal(self, placement: MaskPlacement) -> bool:
        """Resolve hard governance feasibility before any cost comparison."""

        if placement == MaskPlacement.EARLY:
            return self.early_mask_legal
        if not self.late_mask_legal:
            return False
        return self.max_raw_exposure_rows is None or (
            self.join_input_rows <= self.max_raw_exposure_rows
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidatePipelineWork:
    """Concrete logical work quantities for one legal physical candidate."""

    placement: MaskPlacement
    source_scan_payload_bytes: float
    sensitive_derivation_payload_bytes: float
    pre_join_hash_rows: int
    pre_join_hash_payload_bytes: float
    post_join_hash_rows: int
    post_join_hash_payload_bytes: float
    join_fact_payload_bytes: float
    dimension_build_payload_bytes: float
    join_output_rows: int
    boundary_rows: int
    boundary_payload_bytes: float
    output_payload_bytes: float
    sort_key_comparison_bytes: float
    pipeline_breaker: bool
    estimated_raw_join_exposure_rows: int

    def __post_init__(self) -> None:
        numeric = (
            self.source_scan_payload_bytes,
            self.sensitive_derivation_payload_bytes,
            self.pre_join_hash_payload_bytes,
            self.post_join_hash_payload_bytes,
            self.join_fact_payload_bytes,
            self.dimension_build_payload_bytes,
            self.boundary_payload_bytes,
            self.output_payload_bytes,
            self.sort_key_comparison_bytes,
        )
        if any(value < 0.0 or not math.isfinite(value) for value in numeric):
            raise ValueError("Candidate pipeline work must be finite and nonnegative")
        counts = (
            self.pre_join_hash_rows,
            self.post_join_hash_rows,
            self.join_output_rows,
            self.boundary_rows,
            self.estimated_raw_join_exposure_rows,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Candidate pipeline row counts must be nonnegative")

    def feature_vector(self) -> tuple[float, ...]:
        """Return scale-stable candidate features without a learned threshold."""

        mib = float(1024 * 1024)
        return (
            math.log1p(self.source_scan_payload_bytes / mib),
            math.log1p(self.sensitive_derivation_payload_bytes / mib),
            math.log1p(self.pre_join_hash_payload_bytes / mib),
            math.log1p(self.post_join_hash_payload_bytes / mib),
            math.log1p(self.join_fact_payload_bytes / mib),
            math.log1p(self.dimension_build_payload_bytes / mib),
            math.log1p(self.join_output_rows / 100_000.0),
            math.log1p(self.boundary_payload_bytes / mib),
            math.log1p(self.output_payload_bytes / mib),
            math.log1p(self.sort_key_comparison_bytes / mib),
            1.0 if self.pipeline_breaker else 0.0,
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["placement"] = self.placement.value
        payload["feature_names"] = list(V4_WORK_FEATURE_NAMES)
        payload["feature_vector"] = list(self.feature_vector())
        return payload


def derive_candidate_pipeline_work(
    stats: RealPipelineWorkloadStats,
    placement: MaskPlacement,
) -> CandidatePipelineWork:
    """Derive comparable work without making an illegal plan eligible."""

    if not stats.placement_is_legal(placement):
        raise ValueError(f"Mask placement is not governance-feasible: {placement.value}")
    source_scan_payload = stats.source_scan_rows * stats.source_scan_payload_width_bytes
    sensitive_derivation_payload = stats.join_input_rows * stats.sensitive_raw_width_bytes
    dimension_build_payload = stats.dimension_build_rows * stats.dimension_build_payload_width_bytes
    output_row_width = (
        stats.masked_width_bytes
        + stats.output_fixed_width_bytes
        + stats.dimension_output_payload_width_bytes
    )
    output_payload = stats.join_output_rows_estimate * output_row_width
    # Sorting is common in logical meaning but not necessarily in physical
    # latency: a pipeline breaker can change when and how DuckDB schedules it.
    comparisons = (
        stats.join_output_rows_estimate
        * math.log2(max(stats.join_output_rows_estimate, 2))
        * stats.sort_key_width_bytes
    )
    if placement == MaskPlacement.EARLY:
        pre_hash_rows = stats.join_input_rows
        post_hash_rows = 0
        join_width = stats.masked_width_bytes + stats.join_fact_fixed_width_bytes
        boundary_rows = stats.join_input_rows
        boundary_width = join_width
        raw_exposure = 0
        breaker = True
    else:
        pre_hash_rows = 0
        post_hash_rows = stats.join_output_rows_estimate
        join_width = stats.sensitive_raw_width_bytes + stats.join_fact_fixed_width_bytes
        boundary_rows = 0
        boundary_width = 0.0
        raw_exposure = stats.join_input_rows
        breaker = False
    return CandidatePipelineWork(
        placement=placement,
        source_scan_payload_bytes=source_scan_payload,
        sensitive_derivation_payload_bytes=sensitive_derivation_payload,
        pre_join_hash_rows=pre_hash_rows,
        pre_join_hash_payload_bytes=(pre_hash_rows * stats.sensitive_raw_width_bytes),
        post_join_hash_rows=post_hash_rows,
        post_join_hash_payload_bytes=(post_hash_rows * stats.sensitive_raw_width_bytes),
        join_fact_payload_bytes=stats.join_input_rows * join_width,
        dimension_build_payload_bytes=dimension_build_payload,
        join_output_rows=stats.join_output_rows_estimate,
        boundary_rows=boundary_rows,
        boundary_payload_bytes=boundary_rows * boundary_width,
        output_payload_bytes=output_payload,
        sort_key_comparison_bytes=comparisons,
        pipeline_breaker=breaker,
        estimated_raw_join_exposure_rows=raw_exposure,
    )


def candidate_work_delta(
    stats: RealPipelineWorkloadStats,
) -> tuple[float, ...]:
    """Return early minus late features only when both candidates are legal."""

    early = derive_candidate_pipeline_work(stats, MaskPlacement.EARLY).feature_vector()
    late = derive_candidate_pipeline_work(stats, MaskPlacement.LATE).feature_vector()
    return tuple(left - right for left, right in zip(early, late, strict=True))
