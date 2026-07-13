"""Strict Pydantic models for TrustAero IR version 1.0.

Pydantic models are the internal single source of truth. Versioned JSON Schema
files are generated from these models and committed for non-Python consumers.
Cross-node graph validity and policy semantics deliberately live elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, field_validator

from .enums import LineageLevel, ObligationType, PolicyDecision, ReasonCode, ValidationStatus


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


class Filter(OperatorBase):
    operator_type: Literal["Filter"]
    expression: str


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


class Aggregate(OperatorBase):
    operator_type: Literal["Aggregate"]
    group_by: tuple[str, ...]
    aggregates: tuple[str, ...]


class Mask(OperatorBase):
    operator_type: Literal["Mask"]
    fields: tuple[str, ...]
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
