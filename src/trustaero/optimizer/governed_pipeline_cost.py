"""Deployable legality-first selection with the frozen physical-work model."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trustaero.optimizer.candidate_feasibility import (
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.governed_pipeline_space import (
    GovernedPipelineStatistics,
    build_governed_pipeline_candidates,
    plan_governed_pipeline,
)
from trustaero.optimizer.hierarchical_planner import HierarchicalPlanningResult

MODEL_RANKED_LEGAL_CANDIDATE = "PIPELINE_MODEL_RANKED_LEGAL_CANDIDATE"
ONLY_LEGAL_NONDOMINATED_CANDIDATE = "PIPELINE_ONLY_LEGAL_NONDOMINATED_CANDIDATE"
OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK = "PIPELINE_OUT_OF_SUPPORT_FALLBACK"
PIPELINE_NO_LEGAL_CANDIDATE = "PIPELINE_NO_LEGAL_CANDIDATE"

OptimizationStatus = Literal["SELECT", "REJECT"]


@dataclass(frozen=True, slots=True)
class GovernedPipelineModelSupport:
    """Calibration envelope with a finite-sample selectivity tolerance.

    Selectivity factors are proportions estimated from a finite sample.  A
    nominal boundary such as 0.7 must therefore not become an exact floating
    point wall: an observed 0.7001 can be statistically indistinguishable from
    0.7.  ``selectivity_tail_probability`` controls a two-sided Hoeffding
    tolerance derived only from the sample size, never from a holdout result.
    Row count and byte-width limits remain hard boundaries.
    """

    minimum_input_rows: int
    maximum_input_rows: int
    minimum_sensitive_width_bytes: float
    maximum_sensitive_width_bytes: float
    minimum_policy_selectivity: float
    maximum_policy_selectivity: float
    minimum_query_selectivity: float
    maximum_query_selectivity: float
    minimum_join_match_rate: float
    maximum_join_match_rate: float
    selectivity_tail_probability: float = 0.001

    def __post_init__(self) -> None:
        if not 0.0 < self.selectivity_tail_probability < 1.0:
            raise ValueError("Support tail probability must be in (0, 1)")

    def selectivity_tolerance(self, sample_rows: int) -> float:
        """Return a distribution-free two-sided finite-sample tolerance."""

        if sample_rows <= 0:
            return 0.0
        return math.sqrt(math.log(2.0 / self.selectivity_tail_probability) / (2.0 * sample_rows))

    def contains(self, statistics: GovernedPipelineStatistics) -> bool:
        """Return true only when every physical factor is in support."""

        policy = statistics.estimated_policy_rows / statistics.input_rows
        query = statistics.estimated_query_rows / statistics.input_rows
        join_match = (
            statistics.estimated_query_join_rows / statistics.estimated_query_rows
            if statistics.estimated_query_rows
            else 0.0
        )
        input_tolerance = self.selectivity_tolerance(statistics.input_rows)
        join_tolerance = self.selectivity_tolerance(statistics.estimated_query_rows)
        return (
            self.minimum_input_rows <= statistics.input_rows <= self.maximum_input_rows
            and self.minimum_sensitive_width_bytes
            <= statistics.sensitive_width_bytes
            <= self.maximum_sensitive_width_bytes
            and self.minimum_policy_selectivity - input_tolerance
            <= policy
            <= self.maximum_policy_selectivity + input_tolerance
            and self.minimum_query_selectivity - input_tolerance
            <= query
            <= self.maximum_query_selectivity + input_tolerance
            and self.minimum_join_match_rate - join_tolerance
            <= join_match
            <= self.maximum_join_match_rate + join_tolerance
        )


FROZEN_V2_SUPPORT = GovernedPipelineModelSupport(
    minimum_input_rows=50_000,
    maximum_input_rows=120_000,
    minimum_sensitive_width_bytes=128.0,
    maximum_sensitive_width_bytes=1_024.0,
    minimum_policy_selectivity=0.1,
    maximum_policy_selectivity=0.7,
    minimum_query_selectivity=0.5,
    maximum_query_selectivity=0.9,
    minimum_join_match_rate=0.5,
    maximum_join_match_rate=0.9,
)


@dataclass(frozen=True, slots=True)
class FrozenGovernedPipelineCostModel:
    """Immutable additive model loaded from a digest-bound JSON artifact."""

    intercept_ms: float
    coefficients: tuple[tuple[str, float], ...]
    practical_tie_fraction: float
    stable_preference_candidate_id: str
    equivalence_group: str
    model_sha256: str

    def __post_init__(self) -> None:
        if self.intercept_ms < 0.0 or not math.isfinite(self.intercept_ms):
            raise ValueError("Frozen model intercept is invalid")
        names = [name for name, _value in self.coefficients]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Frozen model coefficients must be sorted and unique")
        if any(value < 0.0 or not math.isfinite(value) for _name, value in self.coefficients):
            raise ValueError("Frozen model coefficients must be finite and nonnegative")
        if not 0.0 < self.practical_tie_fraction < 1.0:
            raise ValueError("Frozen model tie fraction is invalid")

    @classmethod
    def from_json(
        cls,
        path: Path | str,
        *,
        expected_sha256: str,
    ) -> FrozenGovernedPipelineCostModel:
        """Load only the exact frozen artifact authorized by evaluation."""

        model_path = Path(path)
        digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ValueError("Frozen governed pipeline model digest changed")
        payload = json.loads(model_path.read_text(encoding="utf-8"))
        if payload.get("development_status") != "AUTHORIZED_FOR_INDEPENDENT_HOLDOUT":
            raise ValueError("Governed pipeline model is not deployment-authorized")
        return cls(
            intercept_ms=float(payload["intercept_ms"]),
            coefficients=tuple(
                sorted((str(name), float(value)) for name, value in payload["coefficients"].items())
            ),
            practical_tie_fraction=float(payload["practical_tie_fraction"]),
            stable_preference_candidate_id=str(payload["stable_preference_candidate_id"]),
            equivalence_group=str(payload["equivalence_group"]),
            model_sha256=digest,
        )

    def predict_ms(self, work_metrics: tuple[tuple[str, float], ...]) -> float:
        """Estimate candidate latency from pre-execution physical work."""

        values = dict(work_metrics)
        return self.intercept_ms + sum(
            coefficient * values.get(name, 0.0) for name, coefficient in self.coefficients
        )


@dataclass(frozen=True, slots=True)
class GovernedPipelineOptimizationDecision:
    """Auditable decision after hard legality and optional model ranking."""

    status: OptimizationStatus
    selected_candidate_id: str | None
    reason_code: str
    feasible_candidate_ids: tuple[str, ...]
    nondominated_candidate_ids: tuple[str, ...]
    predicted_latency_ms: tuple[tuple[str, float], ...]
    predicted_equivalent_candidate_ids: tuple[str, ...]
    performance_model_used: bool
    out_of_support: bool
    planning: HierarchicalPlanningResult


def optimize_governed_pipeline(
    statistics: GovernedPipelineStatistics,
    policy: GovernanceFeasibilityPolicy,
    model: FrozenGovernedPipelineCostModel,
    *,
    support: GovernedPipelineModelSupport = FROZEN_V2_SUPPORT,
) -> GovernedPipelineOptimizationDecision:
    """Filter illegal plans first, then rank only legal non-dominated plans."""

    planning = plan_governed_pipeline(statistics, policy)
    survivors = planning.nondominated_candidate_ids
    if not survivors:
        return GovernedPipelineOptimizationDecision(
            status="REJECT",
            selected_candidate_id=None,
            reason_code=PIPELINE_NO_LEGAL_CANDIDATE,
            feasible_candidate_ids=planning.feasible_candidate_ids,
            nondominated_candidate_ids=(),
            predicted_latency_ms=(),
            predicted_equivalent_candidate_ids=(),
            performance_model_used=False,
            out_of_support=False,
            planning=planning,
        )
    if len(survivors) == 1:
        return GovernedPipelineOptimizationDecision(
            status="SELECT",
            selected_candidate_id=survivors[0],
            reason_code=ONLY_LEGAL_NONDOMINATED_CANDIDATE,
            feasible_candidate_ids=planning.feasible_candidate_ids,
            nondominated_candidate_ids=survivors,
            predicted_latency_ms=(),
            predicted_equivalent_candidate_ids=survivors,
            performance_model_used=False,
            out_of_support=False,
            planning=planning,
        )
    if not support.contains(statistics):
        fallback = planning.selected_candidate_id
        if fallback not in survivors:
            fallback = survivors[0]
        return GovernedPipelineOptimizationDecision(
            status="SELECT",
            selected_candidate_id=fallback,
            reason_code=OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK,
            feasible_candidate_ids=planning.feasible_candidate_ids,
            nondominated_candidate_ids=survivors,
            predicted_latency_ms=(),
            predicted_equivalent_candidate_ids=(fallback,),
            performance_model_used=False,
            out_of_support=True,
            planning=planning,
        )

    profiles = {
        candidate.candidate_id: candidate.profile
        for candidate in build_governed_pipeline_candidates(statistics)
        if candidate.candidate_id in survivors
    }
    predicted = {
        candidate_id: model.predict_ms(profile.work_metrics)
        for candidate_id, profile in profiles.items()
    }
    best = min(predicted.values())
    equivalent = tuple(
        sorted(
            candidate_id
            for candidate_id, latency in predicted.items()
            if latency <= best * (1.0 + model.practical_tie_fraction)
        )
    )
    selected = (
        model.stable_preference_candidate_id
        if model.stable_preference_candidate_id in equivalent
        else min(equivalent, key=lambda item: (predicted[item], item))
    )
    if selected not in planning.feasible_candidate_ids:
        raise ValueError("Cost model attempted to select an illegal candidate")
    return GovernedPipelineOptimizationDecision(
        status="SELECT",
        selected_candidate_id=selected,
        reason_code=MODEL_RANKED_LEGAL_CANDIDATE,
        feasible_candidate_ids=planning.feasible_candidate_ids,
        nondominated_candidate_ids=survivors,
        predicted_latency_ms=tuple(sorted(predicted.items())),
        predicted_equivalent_candidate_ids=equivalent,
        performance_model_used=True,
        out_of_support=False,
        planning=planning,
    )
