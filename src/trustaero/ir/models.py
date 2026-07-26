"""Strict Pydantic models for TrustAero IR version 1.0.

Pydantic models are the internal single source of truth. Versioned JSON Schema
files are generated from these models and committed for non-Python consumers.
Cross-node graph validity and policy semantics deliberately live elsewhere.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    field_validator,
    model_validator,
)

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
            # Exact decimals cross JSON as canonical strings. JSON numbers are
            # commonly parsed as binary floats and would recreate the Q6 bug.
            DataType.DECIMAL: value_type is str,
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
        if self.data_type == DataType.DECIMAL:
            text = str(self.value)
            if re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", text) is None:
                raise ValueError("decimal literal must be a canonical base-10 string")
            try:
                parsed_decimal = Decimal(text)
            except InvalidOperation as exc:  # pragma: no cover - regex is stricter
                raise ValueError("decimal literal is invalid") from exc
            if not parsed_decimal.is_finite():
                raise ValueError("decimal literal must be finite")
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


class NumericProductExpression(StrictModel):
    """Multiply two raw numeric fields inside one aggregate input.

    This deliberately small expression exists for TPC-H Q6 and similarly
    shaped database workloads.  It is *not* a general SQL-expression escape
    hatch: constants, division, nesting and arbitrary functions remain outside
    the reviewed fragment and therefore fail closed.
    """

    expression_type: Literal["numeric_product"]
    left: FieldExpression
    right: FieldExpression


class NumericAffineExpression(StrictModel):
    """Add or subtract one raw numeric field from an exact decimal constant.

    This bounded shape is sufficient for benchmark formulas such as
    ``1 - discount`` and ``1 + tax``. It deliberately forbids nesting,
    division and arbitrary functions, keeping the accepted arithmetic small
    enough to type-check and compile independently.
    """

    expression_type: Literal["numeric_affine"]
    constant: LiteralExpression
    operator: Literal["add", "subtract"]
    field: FieldExpression

    @model_validator(mode="after")
    def constant_must_be_exact_decimal(self) -> NumericAffineExpression:
        if self.constant.data_type != DataType.DECIMAL:
            raise ValueError("numeric-affine constants must use exact DECIMAL literals")
        return self


NumericFormulaFactor = Annotated[
    FieldExpression | NumericAffineExpression,
    Field(discriminator="expression_type"),
]


class NumericProductFormulaExpression(StrictModel):
    """Multiply two or three reviewed numeric factors inside an aggregate."""

    expression_type: Literal["numeric_product_formula"]
    factors: tuple[NumericFormulaFactor, ...] = Field(min_length=2, max_length=3)


class Filter(OperatorBase):
    operator_type: Literal["Filter"]
    expression: PredicateExpression


class Join(OperatorBase):
    operator_type: Literal["Join"]
    left_field: str
    right_field: str
    join_type: Literal["inner", "left"] = "inner"


class SpatialJoin(OperatorBase):
    """Join two point relations using explicitly selected coordinate pairs.

    A relation may contain several spatial descriptors after an earlier join.
    Explicit latitude/longitude pairs keep every later SpatialJoin
    unambiguous.  The current DuckDB execution fragment implements only
    ``distance_within`` over EPSG:4326 points.
    """

    operator_type: Literal["SpatialJoin"]
    relation: Literal["intersects", "within", "distance_within"]
    left_fields: tuple[str, str]
    right_fields: tuple[str, str]
    distance_km: PositiveFloat | None = None

    @model_validator(mode="after")
    def spatial_relation_parameters_must_be_consistent(self) -> SpatialJoin:
        for side, fields in (("left", self.left_fields), ("right", self.right_fields)):
            if any(not field.strip() for field in fields) or len(set(fields)) != 2:
                raise ValueError(
                    f"{side}_fields must contain distinct non-empty latitude and longitude names"
                )
        if self.relation == "distance_within" and self.distance_km is None:
            raise ValueError("distance_within requires distance_km")
        if self.relation != "distance_within" and self.distance_km is not None:
            raise ValueError("distance_km is valid only for distance_within")
        return self


class Project(OperatorBase):
    operator_type: Literal["Project"]
    fields: tuple[str, ...]


class SortKey(StrictModel):
    """One deterministic sort key over a field in the current relation."""

    field: str = Field(min_length=1)
    direction: Literal["asc", "desc"] = "asc"


class Sort(OperatorBase):
    """Order rows by an explicit, non-empty sequence of validated fields."""

    operator_type: Literal["Sort"]
    keys: tuple[SortKey, ...] = Field(min_length=1)


class Limit(OperatorBase):
    """Return at most a reviewed number of rows from an ordered relation.

    The upper bound keeps this operator inside a small, independently
    auditable fragment. Limit never establishes an order itself: benchmark
    adapters must place it after an explicit ``Sort`` for Top-K semantics.
    """

    operator_type: Literal["Limit"]
    row_count: int = Field(ge=1, le=10_000)


class AggregateExpression(StrictModel):
    """One named aggregate result in the supported relational fragment."""

    function: AggregateFunction
    input_field: str | None = None
    input_expression: NumericProductExpression | NumericProductFormulaExpression | None = None
    output_field: str = Field(min_length=1)

    @model_validator(mode="after")
    def input_field_must_match_function(self) -> AggregateExpression:
        inputs = int(self.input_field is not None) + int(self.input_expression is not None)
        if self.function == AggregateFunction.COUNT and inputs != 0:
            raise ValueError("COUNT(*) cannot declare an aggregate input")
        if self.function != AggregateFunction.COUNT and inputs != 1:
            raise ValueError(
                "non-COUNT aggregates require exactly one field or numeric-product input"
            )
        if self.input_expression is not None and self.function not in (
            AggregateFunction.SUM,
            AggregateFunction.AVG,
        ):
            raise ValueError("numeric-expression inputs support SUM and AVG only")
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
    | Sort
    | Limit
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


class OutputFieldSchema(StrictModel):
    """Public final-output field contract after validation and rewrites."""

    name: str = Field(min_length=1)
    data_type: DataType
    nullable: bool = False
    roles: tuple[str, ...] = ()
    sensitive: bool = False
    spatial_precision_km: float | None = Field(default=None, gt=0)
    # Downstream consumers must not treat a masked string as an original value.
    value_state: Literal["raw", "redacted", "hashed", "nullified"] = "raw"


class LineageRequirement(StrictModel):
    """Logical policy requirement; it says what is needed, not how to capture it."""

    level: LineageLevel
    target_operator: str


class LineageInstrumentationSpec(StrictModel):
    """Validated logical-plan instrumentation that a physical plan must realize."""

    level: LineageLevel
    target_operator: str
    capture_operator: str
    capture_mode: Literal["logical_suffix"] = "logical_suffix"


class LineageEvidenceSummary(StrictModel):
    """Execution-time lineage evidence summary for a future certificate check."""

    execution_id: str
    result_id: str
    lineage_level: LineageLevel
    covered_operators: tuple[str, ...]
    edge_digest: str


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
    output_schema: tuple[OutputFieldSchema, ...]
    lineage_requirements: tuple[LineageRequirement, ...] = ()
    lineage_instrumentation: tuple[LineageInstrumentationSpec, ...] = ()
    bindings: SnapshotBindings
    satisfied_obligations: tuple[ObligationType, ...] = ()
    pending_obligations: tuple[ObligationType, ...] = ()
    validation: ValidationSummary


class PhysicalOperatorSpec(StrictModel):
    """Backend-facing operator placeholder derived from a validated operator."""

    physical_operator_id: str
    logical_operator_id: str
    operator_type: str
    inputs: tuple[str, ...] = ()
    backend: Literal["not_bound", "duckdb"] = "not_bound"
    implementation_status: Literal["logical_only", "executable", "requires_backend"] = (
        "logical_only"
    )
    unimplemented_features: tuple[str, ...] = ()


class PhysicalOperatorPlacementSpec(StrictModel):
    """Move one reviewed governance operator after an earlier logical node."""

    operator_id: str = Field(min_length=1)
    after_operator_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def placement_must_move_between_distinct_nodes(self) -> PhysicalOperatorPlacementSpec:
        if self.operator_id == self.after_operator_id:
            raise ValueError("physical placement source and target must differ")
        return self


class PhysicalStrategySpec(StrictModel):
    """Explicit backend strategy decisions that may change physical execution.

    Materialization is a storage boundary, not a logical rewrite: it preserves
    rows and field semantics while changing pipelining and intermediate cost.
    IR v1 permits one ordinary materialization boundary, one bounded
    ordered-filter fragment, or one independently checked Mask placement.  The
    combined placement/materialization mode is deliberately narrower still:
    it may materialize only the Mask that was moved.  This exact shape lets the
    optimizer compare an early masked boundary with a late Mask without
    opening a general-purpose reordering API.
    """

    strategy_id: str = Field(min_length=1)
    execution_mode: Literal[
        "fused",
        "materialized",
        "ordered_materialized",
        "governance_placed",
        "governance_placed_materialized",
    ] = "fused"
    materialize_after: tuple[str, ...] = ()
    filter_order: tuple[str, ...] = ()
    placements: tuple[PhysicalOperatorPlacementSpec, ...] = ()

    @model_validator(mode="after")
    def decision_shape_must_match_mode(self) -> PhysicalStrategySpec:
        if self.execution_mode == "fused":
            if self.materialize_after or self.filter_order or self.placements:
                raise ValueError("fused execution cannot declare physical boundaries")
        elif self.execution_mode == "materialized":
            if len(self.materialize_after) != 1 or self.filter_order or self.placements:
                raise ValueError("IR v1 materialized execution requires exactly one boundary")
        elif self.execution_mode == "ordered_materialized":
            if self.materialize_after or len(self.filter_order) < 2 or self.placements:
                raise ValueError(
                    "ordered materialization requires at least two filters and no other decision"
                )
        elif self.execution_mode == "governance_placed":
            if self.materialize_after or self.filter_order or len(self.placements) != 1:
                raise ValueError(
                    "governance placement requires exactly one placement and no other decision"
                )
        elif (
            len(self.materialize_after) != 1
            or self.filter_order
            or len(self.placements) != 1
            or self.materialize_after[0] != self.placements[0].operator_id
        ):
            raise ValueError(
                "placed materialization must materialize exactly the one moved operator"
            )
        if len(self.filter_order) != len(set(self.filter_order)):
            raise ValueError("ordered filters must be unique")
        return self


class ApprovedPhysicalPlan(StrictModel):
    """Auditable pre-execution physical specification, not executable SQL."""

    schema_version: Literal["1.0"] = "1.0"
    physical_plan_id: str
    logical_plan_id: str
    logical_plan_digest: str
    output_operator: str
    physical_operators: tuple[PhysicalOperatorSpec, ...]
    strategy: PhysicalStrategySpec = Field(
        default_factory=lambda: PhysicalStrategySpec(strategy_id="fused")
    )
    bindings: SnapshotBindings
    lineage_instrumentation: tuple[LineageInstrumentationSpec, ...] = ()
    pending_obligations: tuple[ObligationType, ...] = ()
    unimplemented_backend_features: tuple[str, ...] = ()
    planner_notes: tuple[str, ...] = ()
    # Optional IR-v1 extension. Both values must be supplied together when a
    # physical plan is produced by the hierarchical governed planner.
    planner_decision_digest: str | None = None
    planner_selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def planner_binding_is_complete(self) -> ApprovedPhysicalPlan:
        if (self.planner_decision_digest is None) != (self.planner_selected_candidate_id is None):
            raise ValueError("Physical planner decision binding must be complete")
        return self


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
    lineage_evidence: LineageEvidenceSummary | None = None
    lineage_digest: str | None = None
    planner_decision_digest: str | None = None
    planner_selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def planner_binding_is_complete(self) -> GovernedExecutionCertificate:
        if (self.planner_decision_digest is None) != (self.planner_selected_candidate_id is None):
            raise ValueError("Certificate planner decision binding must be complete")
        return self
