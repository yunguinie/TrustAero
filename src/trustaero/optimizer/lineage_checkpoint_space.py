"""Governance-feasible candidates for reusable record-lineage checkpoints.

Source-level and record-level lineage are intentionally not interchangeable
here.  Every candidate provides the same record-level output-to-source
identity mapping; only capture timing and reuse differ.
"""

from __future__ import annotations

from dataclasses import dataclass

from trustaero.optimizer.candidate_feasibility import CandidateExposure
from trustaero.optimizer.hierarchical_planner import GovernedCandidateProfile

LATE_PER_QUERY_CAPTURE = "late_per_query_capture"
POLICY_LINEAGE_CHECKPOINT = "policy_lineage_checkpoint"
SNAPSHOT_LINEAGE_CHECKPOINT = "snapshot_lineage_checkpoint"
LINEAGE_CHECKPOINT_CANDIDATE_IDS = (
    LATE_PER_QUERY_CAPTURE,
    POLICY_LINEAGE_CHECKPOINT,
    SNAPSHOT_LINEAGE_CHECKPOINT,
)
LINEAGE_CHECKPOINT_EQUIVALENCE_ID = "record-lineage-query-batch-v1"


@dataclass(frozen=True, slots=True)
class LineageCheckpointStatistics:
    """Work estimates for a batch of queries over one frozen snapshot."""

    input_rows: int
    query_count: int
    distinct_policy_count: int
    total_result_rows: int
    total_distinct_policy_rows: int

    def __post_init__(self) -> None:
        if (
            min(
                self.input_rows,
                self.query_count,
                self.distinct_policy_count,
            )
            <= 0
        ):
            raise ValueError("Lineage checkpoint workload sizes must be positive")
        if min(self.total_result_rows, self.total_distinct_policy_rows) < 0:
            raise ValueError("Lineage checkpoint row estimates cannot be negative")
        if self.distinct_policy_count > self.query_count:
            raise ValueError("Distinct policy count exceeds query count")


def build_lineage_checkpoint_profiles(
    statistics: LineageCheckpointStatistics,
) -> tuple[GovernedCandidateProfile, ...]:
    """Represent physical work without prematurely fitting latency weights."""

    input_rows = float(statistics.input_rows)
    queries = float(statistics.query_count)
    policy_count = float(statistics.distinct_policy_count)
    result_rows = float(statistics.total_result_rows)
    policy_rows = float(statistics.total_distinct_policy_rows)

    def profile(
        candidate_id: str,
        *,
        checkpoint_rows: float,
        hash_rows: float,
        source_scan_rows: float,
        checkpoint_scan_rows: float,
        breakers: float,
        provides_checkpoint: bool,
    ) -> GovernedCandidateProfile:
        return GovernedCandidateProfile(
            candidate_id=candidate_id,
            result_equivalence_id=LINEAGE_CHECKPOINT_EQUIVALENCE_ID,
            exposure=CandidateExposure(
                candidate_id=candidate_id,
                raw_rows_exposed_to_join=0,
                raw_rows_materialized=0,
                masked_rows_materialized=int(checkpoint_rows),
                provides_governance_checkpoint=provides_checkpoint,
            ),
            work_metrics=(
                ("checkpoint.payload_bytes", checkpoint_rows * 32.0),
                ("checkpoint.rows", checkpoint_rows),
                ("checkpoint.scan_rows", checkpoint_scan_rows),
                ("lineage.hash_rows", hash_rows),
                ("lineage.output_edges", result_rows),
                ("pipeline_breaker.count", breakers),
                ("source.scan_rows", source_scan_rows),
            ),
        )

    return (
        profile(
            LATE_PER_QUERY_CAPTURE,
            checkpoint_rows=0.0,
            hash_rows=result_rows,
            source_scan_rows=input_rows * queries,
            checkpoint_scan_rows=0.0,
            breakers=0.0,
            provides_checkpoint=False,
        ),
        profile(
            POLICY_LINEAGE_CHECKPOINT,
            checkpoint_rows=policy_rows,
            hash_rows=policy_rows,
            source_scan_rows=input_rows * policy_count,
            checkpoint_scan_rows=policy_rows * queries,
            breakers=policy_count,
            provides_checkpoint=True,
        ),
        profile(
            SNAPSHOT_LINEAGE_CHECKPOINT,
            checkpoint_rows=input_rows,
            hash_rows=input_rows,
            source_scan_rows=input_rows,
            checkpoint_scan_rows=input_rows * queries,
            breakers=1.0,
            provides_checkpoint=True,
        ),
    )
