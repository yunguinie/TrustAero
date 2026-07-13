"""Execution-time lineage evidence checks.

Logical validation can require and plan lineage instrumentation, but only an
execution certificate can prove that lineage evidence was actually produced.
This module keeps that evidence check separate from logical-plan rewriting.
"""

from __future__ import annotations

from dataclasses import dataclass

from trustaero.ir.enums import LineageLevel, ReasonCode
from trustaero.ir.models import (
    Diagnostic,
    LineageEvidenceSummary,
    LineageInstrumentationSpec,
    LineageRequirement,
)


@dataclass(frozen=True)
class LineageEvidenceVerification:
    """Whether execution evidence covers the logical lineage requirements."""

    diagnostics: tuple[Diagnostic, ...]

    @property
    def satisfied(self) -> bool:
        return not self.diagnostics


def _strength(level: LineageLevel) -> int:
    return {
        LineageLevel.NONE: 0,
        LineageLevel.SOURCE: 1,
        LineageLevel.RECORD: 2,
    }[level]


def _diagnostic(code: ReasonCode, message: str, **details: object) -> Diagnostic:
    return Diagnostic(code=code, message=message, details=details)


def verify_lineage_evidence(
    requirements: tuple[LineageRequirement, ...],
    instrumentation: tuple[LineageInstrumentationSpec, ...],
    evidence: LineageEvidenceSummary | None,
) -> LineageEvidenceVerification:
    """Check RequiredLevel <= ImplementedLevel <= ObservedEvidenceLevel.

    The relation is evaluated per target operator. Evidence must cover the same
    target and be at least as strong as the logical policy requirement.
    """

    if not requirements:
        return LineageEvidenceVerification(())
    if evidence is None:
        return LineageEvidenceVerification(
            (
                _diagnostic(
                    ReasonCode.LINEAGE_EVIDENCE_MISSING,
                    "Lineage is required but no execution evidence summary was provided.",
                ),
            )
        )

    diagnostics: list[Diagnostic] = []
    instrumentation_by_target = {item.target_operator: item for item in instrumentation}
    covered = set(evidence.covered_operators)

    for requirement in requirements:
        spec = instrumentation_by_target.get(requirement.target_operator)
        if spec is None:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.LINEAGE_INSTRUMENTATION_MISSING,
                    "Lineage evidence cannot satisfy a target without instrumentation.",
                    target_operator=requirement.target_operator,
                    required_level=requirement.level.value,
                )
            )
            continue
        if _strength(spec.level) < _strength(requirement.level):
            diagnostics.append(
                _diagnostic(
                    ReasonCode.LINEAGE_LEVEL_INSUFFICIENT,
                    "Lineage instrumentation is weaker than the requirement.",
                    target_operator=requirement.target_operator,
                    required_level=requirement.level.value,
                    implemented_level=spec.level.value,
                )
            )
            continue
        if requirement.target_operator not in covered:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.LINEAGE_TARGET_NOT_COVERED,
                    "Lineage evidence does not cover a required output target.",
                    target_operator=requirement.target_operator,
                    covered_operators=sorted(covered),
                )
            )
            continue
        if _strength(evidence.lineage_level) < _strength(requirement.level):
            diagnostics.append(
                _diagnostic(
                    ReasonCode.LINEAGE_LEVEL_INSUFFICIENT,
                    "Observed lineage evidence is weaker than the requirement.",
                    target_operator=requirement.target_operator,
                    required_level=requirement.level.value,
                    evidence_level=evidence.lineage_level.value,
                )
            )
    if not evidence.edge_digest:
        diagnostics.append(
            _diagnostic(
                ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                "Lineage evidence must include a non-empty edge digest.",
            )
        )

    return LineageEvidenceVerification(tuple(diagnostics))
