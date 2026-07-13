"""Strict Pydantic models for TrustAero IR version 1.0.

Pydantic models are the internal single source of truth. Versioned JSON Schema
files are generated from these models and committed for non-Python consumers.
Cross-node graph validity and policy semantics deliberately live elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, field_validator, model_validator

from .enums import (
    AggregateFunction,
    ComparisonOperator,
    DataType,
    LineageLevel,
    ObligationType,
    PolicyDecision,
    ReasonCode,
    ValidationStatus,
)


class StrictModel(BaseModel):
    """Forbid silent input drift and make parsed IR immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Subject(StrictModel):
    subject_id: str
    role: str
    attributes: dict[str, str] = Field(default_factory=dict)


class TimeWindow(StrictModel):
    dimension: Literal["event_time", "valid_time", "publication_time"]
    start: datetime
    end: datetime

    @field_validator("end")
    @classmethod
    def end_must_follow_start(cls, value: datetime, info: Any) -> datetime:
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("end must be later than start")
        return value


class RequestContext(StrictModel):
    subject: Subject
    # Missing purpose is structurally representable so the semantic layer can
    # return CLARIFY rather than collapsing it into a generic parse error.
    purpose: str | None = None
    action: Literal["read", "join", "aggregate", "export"]
    query_time_window: TimeWindow | None = None


class ExportRequest(StrictModel):
    requested: bool = False
    destination: str | None = None
    format: Literal["json", "csv", "parquet"] | None = None


class RequestedOutput(StrictModel):
    fields: tuple[str, ...]
    export: ExportRequest = Field(default_factory=ExportRequest)
    lineage_level: LineageLevel = LineageLevel.NONE


class OperatorBase(StrictModel):
    operator_id: str
    inputs: tuple[str, ...] = ()


class ScanSource(OperatorBase):
    operator_type: Literal["ScanSource"]
    dataset: str
    snapshot: str | None = None


class SpatialFilter(OperatorBase):
    """Select records inside a query area.

    ``radius_km`` controls which records belong to the result. Governance
    rewrites must not reinterpret it as an output-privacy parameter.
    """

    operator_type: Literal["SpatialFilter"]
    center: tuple[float, float]
    radius_km: PositiveFloat
    crs: str = "EPSG:4326"


class TemporalFilter(OperatorBase):
    operator_type: Literal["TemporalFilter"]
    field: str
    start: datetime
    end: datetime


class FieldExpression(StrictModel):
    """Reference a field in the current input relation schema."""

    expression_type: Literal["field"]
    field: str = Field(min_length=1)


class LiteralExpression(StrictModel):
    """A typed scalar constant; datetime values use ISO-8601 strings."""

    expression_type: Literal["literal"]
    data_type: DataType
    value: str | int | float | bool

    @model_validator(mode="after")
    def value_must_match_declared_type(self) -> LiteralExpression:
        """Prevent a plan from lying about a literal's logical type."""

        value_type = type(self.value)
        valid = {
            DataType.STRING: value_type is str,
            DataType.INTEGER: value_type is int,
            DataType.FLOAT: value_type in (int, float),
            DataType.BOOLEAN: value_type is bool,
            DataType.DATETIME: value_type is str,
        }[self.data_type]
        if not valid:
            raise ValueError("literal value does not match its declared data_type")
        if self.data_type == DataType.DATETIME:
            try:
                parsed = datetime.fromisoformat(str(self.value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("datetime literal must be ISO-8601") from exc
            if parsed.utcoffset() is None:
                raise ValueError("datetime literal must include a UTC offset")
        return self


class ComparisonExpression(StrictModel):
    """Compare one bound field with one typed literal.

    Field-to-field comparisons and arithmetic are intentionally absent from
    the first trusted fragment; they require additional coercion semantics.
    """

    expression_type: Literal["comparison"]
    operator: ComparisonOperator
    left: FieldExpression
    right: LiteralExpression
    negated: bool = False


class BooleanExpression(StrictModel):
    """A deliberately flat boolean fragment for deterministic Phase A checks.

    Nested arbitrary expression trees are deferred until the small fragment is
    validated experimentally. ``negated`` represents NOT over the whole group.
    """

    expression_type: Literal["boolean"]
    operator: Literal["and", "or"]
    operands: tuple[ComparisonExpression, ...] = Field(min_length=2)
    negated: bool = False


PredicateExpression = Annotated[
    ComparisonExpression | BooleanExpression,
    Field(discriminator="expression_type"),
]


class Filter(OperatorBase):
    operator_type: Literal["Filter"]
    expression: PredicateExpression


class Join(OperatorBase):
    operator_type: Literal["Join"]
    left_field: str
    right_field: str
    join_type: Literal["inner", "left"] = "inner"


class SpatialJoin(OperatorBase):
    operator_type: Literal["SpatialJoin"]
    relation: Literal["intersects", "within", "distance_within"]
    distance_km: PositiveFloat | None = None


class Project(OperatorBase):
    operator_type: Literal["Project"]
    fields: tuple[str, ...]


class AggregateExpression(StrictModel):
    """One named aggregate result in the supported relational fragment."""

    function: AggregateFunction
    input_field: str | None = None
    output_field: str = Field(min_length=1)

    @model_validator(mode="after")
    def input_field_must_match_function(self) -> AggregateExpression:
        if self.function != AggregateFunction.COUNT and self.input_field is None:
            raise ValueError("non-COUNT aggregates require input_field")
        return self


class Aggregate(OperatorBase):
    operator_type: Literal["Aggregate"]
    group_by: tuple[str, ...]
    aggregates: tuple[AggregateExpression, ...] = Field(min_length=1)


class Mask(OperatorBase):
    operator_type: Literal["Mask"]
    # An empty target would be a no-op and must not satisfy a masking policy.
    fields: tuple[str, ...] = Field(min_length=1)
    method: Literal["redact", "hash", "null"] = "redact"


class GeneralizeLocation(OperatorBase):
    """Reduce disclosed location detail without changing prior selection.

    The logical contract maps the named spatial fields to deterministic fixed
    grid cells of approximately ``precision_km``. It preserves record
    membership: for example, a preceding 50 km SpatialFilter remains a 50 km
    query. Physical coordinate transformation is deliberately deferred to the
    trusted execution layer.
    """

    operator_type: Literal["GeneralizeLocation"]
    fields: tuple[str, ...] = Field(min_length=1)
    precision_km: PositiveFloat
    method: Literal["fixed_grid"] = "fixed_grid"
    preserves_selection: Literal[True] = True


class MinGroupSize(OperatorBase):
    operator_type: Literal["MinGroupSize"]
    minimum_count: int = Field(ge=2)


class LineageCapture(OperatorBase):
    operator_type: Literal["LineageCapture"]
    level: LineageLevel


Operator = Annotated[
    ScanSource
    | SpatialFilter
    | TemporalFilter
    | Filter
    | Join
    | SpatialJoin
    | Project
    | Aggregate
    | Mask
    | GeneralizeLocation
    | MinGroupSize
    | LineageCapture,
    Field(discriminator="operator_type"),
]


class CandidatePlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str
    request_context: RequestContext
    requested_output: RequestedOutput
    operators: tuple[Operator, ...]
    output_operator: str


class SnapshotBindings(StrictModel):
    policy_snapshot: str
    data_snapshots: dict[str, str]


class ValidationSummary(StrictModel):
    rounds: int = Field(ge=1)
    reason_codes: tuple[ReasonCode, ...] = ()
    canonical_digest: str


class ValidatedLogicalPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    logical_plan_id: str
    candidate_plan_id: str
    request_context: RequestContext
    requested_output: RequestedOutput
    operators: tuple[Operator, ...]
    output_operator: str
    bindings: SnapshotBindings
    satisfied_obligations: tuple[ObligationType, ...] = ()
    validation: ValidationSummary


class Obligation(StrictModel):
    obligation_type: ObligationType
    parameters: dict[str, Any] = Field(default_factory=dict)


class PolicyRule(StrictModel):
    policy_id: str
    policy_version: str
    subject_roles: tuple[str, ...]
    purposes: tuple[str, ...]
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    decision: PolicyDecision
    obligations: tuple[Obligation, ...] = ()
    reason: str


class PolicySet(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_set_id: str
    policy_snapshot: str
    rules: tuple[PolicyRule, ...]


class Diagnostic(StrictModel):
    code: ReasonCode
    message: str
    operator_id: str | None = None
    policy_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidatorResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    status: ValidationStatus
    candidate_plan_id: str | None = None
    policy_decision: PolicyDecision | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    validated_plan: ValidatedLogicalPlan | None = None


class ExecutionEvent(StrictModel):
    sequence: int = Field(ge=0)
    event_type: Literal[
        "PlanApproved",
        "OperatorStarted",
        "OperatorCompleted",
        "PolicyDecisionRecorded",
        "ResultMaterialized",
        "LineageRecorded",
        "CertificateEmitted",
    ]
    operator_id: str | None = None
    payload_digest: str


class GovernedExecutionCertificate(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    certificate_id: str
    task_digest: str
    logical_plan_id: str
    physical_plan_id: str
    policy_snapshot: str
    data_snapshots: dict[str, str]
    events: tuple[ExecutionEvent, ...]
    result_digest: str
    lineage_digest: str | None = None
