"""Minimal, measured source-lineage instrumentation for DuckDB V1.

This module intentionally does not claim record provenance. It binds a result
to the governed dataset snapshots reachable from each required output target.
That small claim is independently checkable and has a real measured capture
cost; stronger lineage levels fail closed until row identities are propagated.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from trustaero.ir.enums import LineageLevel
from trustaero.ir.models import (
    LineageEvidenceSummary,
    Operator,
    ScanSource,
    ValidatedLogicalPlan,
)


class LineageInstrumentationError(ValueError):
    """Raised when a plan requests lineage outside the implemented fragment."""


@dataclass(frozen=True)
class SourceLineageCaptureResult:
    """Source evidence plus the independently measured instrumentation cost."""

    evidence: LineageEvidenceSummary | None
    lineage_digest: str | None
    latency_ms: float
    source_count: int


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _reachable_scans(
    target: str,
    operators: dict[str, Operator],
) -> tuple[ScanSource, ...]:
    """Return the unique ScanSource ancestors of one lineage target."""

    found: dict[str, ScanSource] = {}
    visited: set[str] = set()

    def visit(operator_id: str) -> None:
        if operator_id in visited:
            return
        visited.add(operator_id)
        operator = operators.get(operator_id)
        if operator is None:
            raise LineageInstrumentationError(
                f"Lineage target references an unknown operator: {operator_id}"
            )
        if isinstance(operator, ScanSource):
            found[operator.operator_id] = operator
            return
        for input_id in operator.inputs:
            visit(input_id)

    visit(target)
    return tuple(found[key] for key in sorted(found))


def capture_source_lineage(
    plan: ValidatedLogicalPlan,
    *,
    execution_id: str,
    result_id: str,
) -> SourceLineageCaptureResult:
    """Capture source-snapshot edges for each required output target.

    No requirements means no evidence and essentially zero extra work. Record
    requirements are rejected explicitly rather than downgraded to source
    lineage, because that would make a certificate overstate what was observed.
    """

    started = time.perf_counter()
    requirements = plan.lineage_requirements
    if not requirements:
        return SourceLineageCaptureResult(None, None, 0.0, 0)
    if any(requirement.level != LineageLevel.SOURCE for requirement in requirements):
        raise LineageInstrumentationError(
            "DuckDB V1 source instrumentation cannot satisfy record-level lineage."
        )

    operators = {operator.operator_id: operator for operator in plan.operators}
    edges: list[dict[str, str]] = []
    sources: set[tuple[str, str]] = set()
    targets: list[str] = []
    for requirement in requirements:
        targets.append(requirement.target_operator)
        scans = _reachable_scans(requirement.target_operator, operators)
        if not scans:
            raise LineageInstrumentationError(
                f"Lineage target has no reachable data source: {requirement.target_operator}"
            )
        for scan in scans:
            snapshot = plan.bindings.data_snapshots.get(scan.dataset)
            if snapshot is None:
                raise LineageInstrumentationError(
                    f"Lineage source has no resolved snapshot: {scan.dataset}"
                )
            sources.add((scan.dataset, snapshot))
            edges.append(
                {
                    "dataset": scan.dataset,
                    "snapshot": snapshot,
                    "target_operator": requirement.target_operator,
                }
            )

    canonical_edges = sorted(edges, key=lambda item: tuple(item.values()))
    edge_digest = _digest(canonical_edges)
    payload = {
        "execution_id": execution_id,
        "result_id": result_id,
        "logical_plan_digest": plan.validation.canonical_digest,
        "edges": canonical_edges,
    }
    evidence = LineageEvidenceSummary(
        execution_id=execution_id,
        result_id=result_id,
        lineage_level=LineageLevel.SOURCE,
        covered_operators=tuple(dict.fromkeys(targets)),
        edge_digest=edge_digest,
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    return SourceLineageCaptureResult(
        evidence=evidence,
        lineage_digest=_digest(payload),
        latency_ms=latency_ms,
        source_count=len(sources),
    )
