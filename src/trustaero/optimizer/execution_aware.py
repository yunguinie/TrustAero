"""Execution-aware cost representation for policy-legal physical candidates.

The older Mask optimizers summarized a whole query with one row width.  EA-0
showed that this is not faithful to DuckDB: column pruning can keep a wide
sensitive value out of a Join, and a materialization boundary can change how a
downstream aggregate executes.  This module therefore represents the work of
*each candidate* at each physical stage.

No fitted parameters live here.  Governance feasibility is evaluated first,
then an independently calibrated analytic model prices the remaining legal
work.  Missing rates and out-of-range features fail closed or use an explicit
conservative fallback; they never make an illegal candidate selectable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    CandidateFeasibilityResult,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)

GIB = float(1024**3)
MILLION = 1_000_000.0

OperatorInputMode = Literal[
    "none",
    "streaming",
    "fused_expression",
    "materialized_input",
]
AggregateWorkKind = Literal["none", "simple", "raw_length", "masked_digest"]
StatisticProvenance = Literal[
    "planner_derived",
    "catalog_exact_controlled",
    "catalog_estimate",
]


@dataclass(frozen=True, slots=True)
class ActiveColumn:
    """One column that a physical stage actually needs.

    ``width_bytes`` is a catalog or controlled logical estimate.  It is not an
    engine-reported number of bytes copied internally by DuckDB.  Keeping the
    name and value state prevents a masked 64-byte string from being confused
    with the original sensitive value merely because both have a string type.
    """

    name: str
    width_bytes: float
    value_state: Literal["raw", "redacted", "hashed", "nullified"] = "raw"
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Active column name cannot be empty")
        if self.width_bytes < 0.0 or not math.isfinite(self.width_bytes):
            raise ValueError("Active column width must be finite and nonnegative")


def _validate_columns(label: str, columns: tuple[ActiveColumn, ...]) -> None:
    names = [column.name for column in columns]
    if len(names) != len(set(names)):
        raise ValueError(f"{label} active columns must be unique")


def _width(columns: tuple[ActiveColumn, ...]) -> float:
    return sum(column.width_bytes for column in columns)


@dataclass(frozen=True, slots=True)
class ExecutionAwareCandidateSpec:
    """Trusted pre-execution statistics for one already generated candidate.

    The planner/adapter, rather than candidate-authored metadata, must derive
    these fields.  Separate active-column sets are intentional: a sensitive
    column present in the scan or final output does not automatically belong
    to the Join payload.
    """

    candidate_id: str
    physical_plan_id: str
    statistic_provenance: StatisticProvenance
    scan_rows: int
    scan_columns: tuple[ActiveColumn, ...]
    join_build_rows: int
    join_build_columns: tuple[ActiveColumn, ...]
    join_probe_rows: int
    join_probe_columns: tuple[ActiveColumn, ...]
    join_output_rows: int
    join_output_columns: tuple[ActiveColumn, ...]
    mask_rows: int = 0
    mask_input_columns: tuple[ActiveColumn, ...] = ()
    mask_mode: OperatorInputMode = "none"
    raw_materialization_rows: int = 0
    raw_materialization_columns: tuple[ActiveColumn, ...] = ()
    masked_materialization_rows: int = 0
    masked_materialization_columns: tuple[ActiveColumn, ...] = ()
    aggregate_input_rows: int = 0
    aggregate_input_columns: tuple[ActiveColumn, ...] = ()
    aggregate_mode: OperatorInputMode = "none"
    aggregate_work_kind: AggregateWorkKind = "none"
    sort_rows: int = 0
    sort_key_columns: tuple[ActiveColumn, ...] = ()
    sort_mode: OperatorInputMode = "none"
    result_rows: int = 0
    result_columns: tuple[ActiveColumn, ...] = ()
    lineage_rows: int = 0
    lineage_edges: int = 0
    lineage_payload_width_bytes: float = 0.0
    pipeline_breaker_kinds: tuple[str, ...] = ()
    exposure: CandidateExposure | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.physical_plan_id.strip():
            raise ValueError("Candidate and physical plan IDs cannot be empty")
        counts = (
            self.scan_rows,
            self.join_build_rows,
            self.join_probe_rows,
            self.join_output_rows,
            self.mask_rows,
            self.raw_materialization_rows,
            self.masked_materialization_rows,
            self.aggregate_input_rows,
            self.sort_rows,
            self.result_rows,
            self.lineage_rows,
            self.lineage_edges,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Execution-aware row and edge counts cannot be negative")
        if self.lineage_payload_width_bytes < 0.0 or not math.isfinite(
            self.lineage_payload_width_bytes
        ):
            raise ValueError("Lineage payload width must be finite and nonnegative")
        column_sets = {
            "scan": self.scan_columns,
            "Join build": self.join_build_columns,
            "Join probe": self.join_probe_columns,
            "Join output": self.join_output_columns,
            "Mask input": self.mask_input_columns,
            "raw materialization": self.raw_materialization_columns,
            "masked materialization": self.masked_materialization_columns,
            "aggregate input": self.aggregate_input_columns,
            "sort key": self.sort_key_columns,
            "result": self.result_columns,
        }
        for label, columns in column_sets.items():
            _validate_columns(label, columns)
        for rows, columns, label in (
            (self.scan_rows, self.scan_columns, "scan"),
            (self.join_build_rows, self.join_build_columns, "Join build"),
            (self.join_probe_rows, self.join_probe_columns, "Join probe"),
            (self.join_output_rows, self.join_output_columns, "Join output"),
            (self.mask_rows, self.mask_input_columns, "Mask"),
            (
                self.raw_materialization_rows,
                self.raw_materialization_columns,
                "raw materialization",
            ),
            (
                self.masked_materialization_rows,
                self.masked_materialization_columns,
                "masked materialization",
            ),
            (self.aggregate_input_rows, self.aggregate_input_columns, "aggregate"),
            (self.sort_rows, self.sort_key_columns, "sort"),
            (self.result_rows, self.result_columns, "result"),
        ):
            if rows > 0 and not columns:
                raise ValueError(f"Nonempty {label} work requires active columns")
        for rows, mode, label in (
            (self.mask_rows, self.mask_mode, "Mask"),
            (self.aggregate_input_rows, self.aggregate_mode, "aggregate"),
            (self.sort_rows, self.sort_mode, "sort"),
        ):
            if (rows == 0) != (mode == "none"):
                raise ValueError(f"{label} mode must agree with its row count")
        if (self.aggregate_input_rows == 0) != (self.aggregate_work_kind == "none"):
            raise ValueError("Aggregate work kind must agree with its row count")
        if len(self.pipeline_breaker_kinds) != len(set(self.pipeline_breaker_kinds)):
            raise ValueError("Pipeline breaker kinds must be unique")
        if any(not value.strip() for value in self.pipeline_breaker_kinds):
            raise ValueError("Pipeline breaker kind cannot be empty")
        if self.exposure is None or self.exposure.candidate_id != self.candidate_id:
            raise ValueError("Candidate exposure must be trusted and ID-bound")
        raw_join_columns = self.join_probe_columns + self.join_output_columns
        raw_sensitive_reaches_join = any(
            column.sensitive and column.value_state == "raw" for column in raw_join_columns
        )
        if raw_sensitive_reaches_join and self.exposure.raw_rows_exposed_to_join <= 0:
            raise ValueError("Raw sensitive Join work requires nonzero governance exposure")
        raw_sensitive_materialized = any(
            column.sensitive and column.value_state == "raw"
            for column in self.raw_materialization_columns
        )
        if (
            raw_sensitive_materialized
            and self.exposure.raw_rows_materialized < self.raw_materialization_rows
        ):
            raise ValueError("Raw materialization exposure understates active sensitive rows")
        masked_sensitive_materialized = any(
            column.sensitive and column.value_state != "raw"
            for column in self.masked_materialization_columns
        )
        if (
            masked_sensitive_materialized
            and self.exposure.masked_rows_materialized < self.masked_materialization_rows
        ):
            raise ValueError("Masked materialization exposure understates active rows")


@dataclass(frozen=True, slots=True)
class ExecutionAwareWorkVector:
    """Sparse physical work features with explicit units and provenance."""

    candidate_id: str
    physical_plan_id: str
    statistic_provenance: StatisticProvenance
    features: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        names = [name for name, _value in self.features]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Execution-aware features must be sorted and unique")
        if any(value < 0.0 or not math.isfinite(value) for _name, value in self.features):
            raise ValueError("Execution-aware features must be finite and nonnegative")

    def as_dict(self) -> dict[str, float]:
        return dict(self.features)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "physical_plan_id": self.physical_plan_id,
            "statistic_provenance": self.statistic_provenance,
            "features": self.as_dict(),
            "byte_semantics": "logical active-column estimates, not DuckDB copy telemetry",
        }


def derive_execution_aware_work(
    spec: ExecutionAwareCandidateSpec,
) -> ExecutionAwareWorkVector:
    """Translate one trusted candidate into auditable analytic cost units."""

    features: dict[str, float] = {
        "scan.payload_gib": spec.scan_rows * _width(spec.scan_columns) / GIB,
        "join.build_rows_million": spec.join_build_rows / MILLION,
        "join.build_payload_gib": (spec.join_build_rows * _width(spec.join_build_columns) / GIB),
        "join.probe_rows_million": spec.join_probe_rows / MILLION,
        "join.probe_payload_gib": (spec.join_probe_rows * _width(spec.join_probe_columns) / GIB),
        "join.output_rows_million": spec.join_output_rows / MILLION,
        "join.output_payload_gib": (spec.join_output_rows * _width(spec.join_output_columns) / GIB),
        "materialization.raw.write_gib": (
            spec.raw_materialization_rows * _width(spec.raw_materialization_columns) / GIB
        ),
        "materialization.raw.read_gib": (
            spec.raw_materialization_rows * _width(spec.raw_materialization_columns) / GIB
        ),
        "materialization.masked.write_gib": (
            spec.masked_materialization_rows * _width(spec.masked_materialization_columns) / GIB
        ),
        "materialization.masked.read_gib": (
            spec.masked_materialization_rows * _width(spec.masked_materialization_columns) / GIB
        ),
        "output.payload_gib": spec.result_rows * _width(spec.result_columns) / GIB,
        "lineage.rows_million": spec.lineage_rows / MILLION,
        "lineage.edges_million": spec.lineage_edges / MILLION,
        "lineage.payload_gib": (spec.lineage_rows * spec.lineage_payload_width_bytes / GIB),
    }
    if spec.mask_rows:
        features[f"mask.{spec.mask_mode}.rows_million"] = spec.mask_rows / MILLION
        features[f"mask.{spec.mask_mode}.input_gib"] = (
            spec.mask_rows * _width(spec.mask_input_columns) / GIB
        )
    if spec.aggregate_input_rows:
        aggregate_prefix = f"aggregate.{spec.aggregate_mode}.{spec.aggregate_work_kind}"
        features[f"{aggregate_prefix}.rows_million"] = spec.aggregate_input_rows / MILLION
        features[f"{aggregate_prefix}.input_gib"] = (
            spec.aggregate_input_rows * _width(spec.aggregate_input_columns) / GIB
        )
    if spec.sort_rows:
        comparisons = spec.sort_rows * math.log2(max(spec.sort_rows, 2))
        features[f"sort.{spec.sort_mode}.comparison_gib"] = (
            comparisons * _width(spec.sort_key_columns) / GIB
        )
    for kind in spec.pipeline_breaker_kinds:
        features[f"pipeline_breaker.{kind}.count"] = 1.0
    return ExecutionAwareWorkVector(
        candidate_id=spec.candidate_id,
        physical_plan_id=spec.physical_plan_id,
        statistic_provenance=spec.statistic_provenance,
        features=tuple(sorted(features.items())),
    )


@dataclass(frozen=True, slots=True)
class AnalyticFeatureRate:
    """Calibrated milliseconds per one named work unit."""

    feature_name: str
    milliseconds_per_unit: float

    def __post_init__(self) -> None:
        if not self.feature_name.strip():
            raise ValueError("Analytic feature name cannot be empty")
        if self.milliseconds_per_unit < 0.0 or not math.isfinite(self.milliseconds_per_unit):
            raise ValueError("Analytic feature rate must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class FeatureSupportBound:
    """Calibration range for one work quantity, including its physical unit."""

    feature_name: str
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if not self.feature_name.strip() or self.minimum < 0.0:
            raise ValueError("Feature support name and minimum are invalid")
        if self.maximum < self.minimum or not math.isfinite(self.maximum):
            raise ValueError("Feature support maximum is invalid")


@dataclass(frozen=True, slots=True)
class AnalyticExecutionCostModel:
    """Serializable, component-wise cost model calibrated independently."""

    calibration_id: str
    rates: tuple[AnalyticFeatureRate, ...]
    support_bounds: tuple[FeatureSupportBound, ...]
    stable_legal_preference: tuple[str, ...]
    practical_tie_fraction: float = 0.03
    intercept_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.calibration_id.strip() or not self.rates:
            raise ValueError("Analytic model requires a calibration ID and rates")
        rate_names = [item.feature_name for item in self.rates]
        bound_names = [item.feature_name for item in self.support_bounds]
        if len(rate_names) != len(set(rate_names)):
            raise ValueError("Analytic feature rates must be unique")
        if len(bound_names) != len(set(bound_names)) or set(rate_names) != set(bound_names):
            raise ValueError("Every analytic feature rate requires one support bound")
        if not self.stable_legal_preference or len(self.stable_legal_preference) != len(
            set(self.stable_legal_preference)
        ):
            raise ValueError("Stable legal candidate preference must be nonempty and unique")
        if not 0.0 <= self.practical_tie_fraction < 1.0:
            raise ValueError("Practical tie fraction must be in [0, 1)")
        if self.intercept_ms < 0.0 or not math.isfinite(self.intercept_ms):
            raise ValueError("Analytic query intercept must be finite and nonnegative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "execution_aware_analytic_cost_v1",
            "calibration_id": self.calibration_id,
            "rates": [asdict(item) for item in self.rates],
            "support_bounds": [asdict(item) for item in self.support_bounds],
            "stable_legal_preference": list(self.stable_legal_preference),
            "practical_tie_fraction": self.practical_tie_fraction,
            "intercept_ms": self.intercept_ms,
            "direct_winner_classifier_used": False,
        }


@dataclass(frozen=True, slots=True)
class CandidateCostEstimate:
    candidate_id: str
    total_ms: float
    component_costs_ms: tuple[tuple[str, float], ...]
    within_calibration_support: bool


def estimate_execution_cost(
    work: ExecutionAwareWorkVector,
    model: AnalyticExecutionCostModel,
) -> CandidateCostEstimate:
    """Price every positive work feature and reject a missing calibration."""

    rates = {item.feature_name: item.milliseconds_per_unit for item in model.rates}
    bounds = {item.feature_name: item for item in model.support_bounds}
    components: list[tuple[str, float]] = [("query.intercept_ms", model.intercept_ms)]
    within = True
    for name, value in work.features:
        if value > 0.0 and name not in rates:
            raise ValueError(f"No calibrated analytic rate for positive feature: {name}")
        if name not in rates:
            continue
        bound = bounds[name]
        within = within and bound.minimum <= value <= bound.maximum
        components.append((name, value * rates[name]))
    return CandidateCostEstimate(
        candidate_id=work.candidate_id,
        total_ms=sum(value for _name, value in components),
        component_costs_ms=tuple(components),
        within_calibration_support=within,
    )


@dataclass(frozen=True, slots=True)
class ExecutionAwareRankingResult:
    """Auditable result after legality, support, cost, and tie handling."""

    status: Literal["SELECT", "REJECT"]
    selected_candidate_id: str | None
    reason_code: str
    feasible_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]
    practically_tied_candidate_ids: tuple[str, ...]
    estimates: tuple[CandidateCostEstimate, ...]
    feasibility: CandidateFeasibilityResult


def _stable_choice(candidate_ids: tuple[str, ...], model: AnalyticExecutionCostModel) -> str:
    for candidate_id in model.stable_legal_preference:
        if candidate_id in candidate_ids:
            return candidate_id
    return sorted(candidate_ids)[0]


def rank_execution_aware_candidates(
    specs: tuple[ExecutionAwareCandidateSpec, ...],
    policy: GovernanceFeasibilityPolicy,
    model: AnalyticExecutionCostModel,
) -> ExecutionAwareRankingResult:
    """Filter illegal candidates before touching any cost-model feature."""

    if not specs:
        raise ValueError("Execution-aware ranking requires at least one candidate")
    candidate_ids = [item.candidate_id for item in specs]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Execution-aware candidate IDs must be unique")
    exposures = tuple(cast_exposure(item) for item in specs)
    feasibility = filter_feasible_candidates(exposures, policy)
    if feasibility.status == "REJECT":
        return ExecutionAwareRankingResult(
            status="REJECT",
            selected_candidate_id=None,
            reason_code="EXECUTION_AWARE_NO_LEGAL_CANDIDATE",
            feasible_candidate_ids=(),
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            practically_tied_candidate_ids=(),
            estimates=(),
            feasibility=feasibility,
        )
    legal_ids = set(feasibility.feasible_candidate_ids)
    legal_specs = tuple(item for item in specs if item.candidate_id in legal_ids)
    if len(legal_specs) == 1:
        only = legal_specs[0].candidate_id
        return ExecutionAwareRankingResult(
            status="SELECT",
            selected_candidate_id=only,
            reason_code="EXECUTION_AWARE_ONLY_LEGAL_CANDIDATE",
            feasible_candidate_ids=(only,),
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            practically_tied_candidate_ids=(only,),
            estimates=(),
            feasibility=feasibility,
        )
    estimates = tuple(
        estimate_execution_cost(derive_execution_aware_work(item), model) for item in legal_specs
    )
    if not all(item.within_calibration_support for item in estimates):
        selected = _stable_choice(feasibility.feasible_candidate_ids, model)
        return ExecutionAwareRankingResult(
            status="SELECT",
            selected_candidate_id=selected,
            reason_code="EXECUTION_AWARE_OUT_OF_SUPPORT_FALLBACK",
            feasible_candidate_ids=feasibility.feasible_candidate_ids,
            rejected_candidate_ids=feasibility.rejected_candidate_ids,
            # Out-of-support is epistemic uncertainty, not evidence of a tie.
            practically_tied_candidate_ids=(),
            estimates=estimates,
            feasibility=feasibility,
        )
    best = min(item.total_ms for item in estimates)
    tied = tuple(
        sorted(
            item.candidate_id
            for item in estimates
            if item.total_ms <= best * (1.0 + model.practical_tie_fraction)
        )
    )
    if len(tied) > 1:
        selected = _stable_choice(tied, model)
        reason = "EXECUTION_AWARE_PRACTICAL_TIE_FALLBACK"
    else:
        selected = tied[0]
        reason = "EXECUTION_AWARE_MINIMUM_ANALYTIC_COST"
    return ExecutionAwareRankingResult(
        status="SELECT",
        selected_candidate_id=selected,
        reason_code=reason,
        feasible_candidate_ids=feasibility.feasible_candidate_ids,
        rejected_candidate_ids=feasibility.rejected_candidate_ids,
        practically_tied_candidate_ids=tied,
        estimates=estimates,
        feasibility=feasibility,
    )


def cast_exposure(spec: ExecutionAwareCandidateSpec) -> CandidateExposure:
    """Narrow the validated optional field after dataclass construction checks."""

    if spec.exposure is None:  # Defensive for non-standard deserializers.
        raise ValueError("Execution-aware candidate is missing trusted exposure")
    return spec.exposure
