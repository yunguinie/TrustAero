"""Typed records used by the Phase 0 experiment runner.

Phase 0 measures TrustAero's validator and certificate-checking semantics. It
does not measure real DBMS execution, optimizer choices, or lineage backend
costs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExperimentCase:
    """One repeatable validation case from the experiment matrix."""

    case_id: str
    case_category: str
    case_kind: str
    scenario: str
    plan_path: str
    policy_path: str
    catalog_path: str
    expected_status: str
    expected_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseResult:
    """Flattened per-case output row written to ``cases.csv``."""

    run_id: str
    commit_hash: str
    case_id: str
    case_category: str
    case_kind: str
    scenario: str
    expected_status: str
    actual_status: str
    status_correct: bool
    expected_reason_codes: tuple[str, ...]
    actual_reason_codes: tuple[str, ...]
    reason_code_correct: bool
    runs: int
    warmup_runs: int
    cold_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    plan_size_bytes: int
    operator_count: int
    edge_count: int
    rewrite_rounds: int | None
    inserted_operator_count: int
    pending_obligation_count: int
    verified_obligation_count: int
    certificate_event_count: int
    plan_digest: str


@dataclass(frozen=True)
class Phase0Config:
    """Runner settings that should be saved with every result directory."""

    cases_path: str
    results_dir: str
    warmup_runs: int = 5
    measured_runs: int = 30


@dataclass(frozen=True)
class Phase0RunSummary:
    """One summarized Phase 0 run, suitable for a paper-facing CSV table."""

    run_id: str
    commit_hash: str
    case_count: int
    status_accuracy: float
    reason_code_accuracy: float
    detection_rate: float
    false_reject_rate: float
    median_latency_ms: float
    p95_latency_ms: float
    max_latency_ms: float
    all_correct: bool
    failed_cases: tuple[str, ...]


@dataclass(frozen=True)
class Phase0CategorySummary:
    """Aggregated correctness and latency metrics for one case category."""

    run_id: str
    case_category: str
    case_count: int
    status_correct: int
    reason_code_correct: int
    median_latency_ms: float
    p95_latency_ms: float
    failed_cases: tuple[str, ...]


@dataclass(frozen=True)
class Phase0ReasonCodeSummary:
    """Coverage count for one expected or observed diagnostic reason code."""

    run_id: str
    reason_code: str
    expected_count: int
    actual_count: int
    matched_count: int
