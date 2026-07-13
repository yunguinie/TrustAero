"""Three-layer validator: L1 structure, L2 graph, and L3 governance.

核心函数尽量保持纯函数风格：相同输入和上下文必须得到相同输出。异常、未知
引用和无法确定的授权都不能默认为 ACCEPT。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from trustaero.catalog.models import Catalog
from trustaero.ir.enums import ObligationType, PolicyDecision, ReasonCode, ValidationStatus
from trustaero.ir.models import (
    Aggregate,
    CandidatePlan,
    Diagnostic,
    GeneralizeLocation,
    LineageCapture,
    Mask,
    MinGroupSize,
    Obligation,
    Operator,
    PolicySet,
    SnapshotBindings,
    ValidatedLogicalPlan,
    ValidationSummary,
    ValidatorResponse,
)
from trustaero.policy.evaluator import evaluate_policy
from trustaero.validator.obligation_normalizer import normalize_obligations
from trustaero.validator.obligations import verify_obligations
from trustaero.validator.type_checker import type_check_plan


def _diagnostic(code: ReasonCode, message: str, **details: Any) -> Diagnostic:
    return Diagnostic(code=code, message=message, details=details)


def parse_candidate(raw: dict[str, Any]) -> CandidatePlan | ValidatorResponse:
    """L1: parse strict structure while preserving stable public reason codes."""

    try:
        return CandidatePlan.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        unknown_operator = any(error.get("type") == "union_tag_invalid" for error in errors)
        code = ReasonCode.UNKNOWN_OPERATOR if unknown_operator else ReasonCode.PLAN_PARSE_ERROR
        plan_id = raw.get("plan_id") if isinstance(raw.get("plan_id"), str) else None
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan_id,
            diagnostics=(
                _diagnostic(
                    code, "Candidate plan failed strict structural validation.", errors=errors
                ),
            ),
        )


def validate_graph(plan: CandidatePlan) -> tuple[Diagnostic, ...]:
    """L2: validate IDs, references, reachability, and acyclicity.

    This check is fail-closed: graph errors prevent governance evaluation because
    policy analysis over an ill-defined dataflow would be unsound.
    """

    diagnostics: list[Diagnostic] = []
    # Input arity is an operator semantic, not a JSON shape constraint. Keeping
    # it here produces stable diagnostics instead of exposing Pydantic internals.
    expected_inputs = {
        "ScanSource": 0,
        "SpatialFilter": 1,
        "TemporalFilter": 1,
        "Filter": 1,
        "Join": 2,
        "SpatialJoin": 2,
        "Project": 1,
        "Aggregate": 1,
        "Mask": 1,
        "GeneralizeLocation": 1,
        "MinGroupSize": 1,
        "LineageCapture": 1,
    }
    for operator in plan.operators:
        expected = expected_inputs[operator.operator_type]
        if len(operator.inputs) != expected:
            diagnostics.append(
                Diagnostic(
                    code=ReasonCode.INVALID_OPERATOR_ARGUMENT,
                    message="Operator has an invalid number of inputs.",
                    operator_id=operator.operator_id,
                    details={"expected": expected, "actual": len(operator.inputs)},
                )
            )
    ids = [operator.operator_id for operator in plan.operators]
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        diagnostics.append(
            _diagnostic(
                ReasonCode.DUPLICATE_OPERATOR_ID, "Operator IDs must be unique.", ids=duplicates
            )
        )
    id_set = set(ids)
    if plan.output_operator not in id_set:
        diagnostics.append(
            _diagnostic(ReasonCode.OUTPUT_OPERATOR_UNKNOWN, "Output operator does not exist.")
        )
    for operator in plan.operators:
        missing = sorted(set(operator.inputs) - id_set)
        if missing:
            diagnostics.append(
                Diagnostic(
                    code=ReasonCode.UNBOUND_REFERENCE,
                    message="Operator references unknown inputs.",
                    operator_id=operator.operator_id,
                    details={"inputs": missing},
                )
            )

    # DFS colors: 0 unseen, 1 visiting, 2 finished. A back edge means a cycle.
    by_id = {operator.operator_id: operator for operator in plan.operators}
    color: dict[str, int] = {}

    def visit(operator_id: str) -> bool:
        state = color.get(operator_id, 0)
        if state == 1:
            return False
        if state == 2 or operator_id not in by_id:
            return True
        color[operator_id] = 1
        if not all(visit(parent) for parent in by_id[operator_id].inputs):
            return False
        color[operator_id] = 2
        return True

    if any(not visit(operator_id) for operator_id in ids):
        diagnostics.append(_diagnostic(ReasonCode.CYCLIC_PLAN, "Plan graph must be acyclic."))

    if plan.output_operator in by_id:
        reachable: set[str] = set()

        def collect(operator_id: str) -> None:
            if operator_id in reachable or operator_id not in by_id:
                return
            reachable.add(operator_id)
            for parent in by_id[operator_id].inputs:
                collect(parent)

        collect(plan.output_operator)
        unreachable = sorted(id_set - reachable)
        if unreachable:
            diagnostics.append(
                _diagnostic(
                    ReasonCode.UNREACHABLE_OPERATOR,
                    "Every operator must contribute to the declared output.",
                    ids=unreachable,
                )
            )
    return tuple(diagnostics)


def _canonical_digest(
    plan: CandidatePlan,
    operators: tuple[Operator, ...],
    output_operator: str,
) -> str:
    """Hash the complete rewritten logical shape, including its final output."""

    payload = plan.model_dump(mode="json")
    payload["operators"] = [operator.model_dump(mode="json") for operator in operators]
    payload["output_operator"] = output_operator
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _output_depends_on_aggregate(plan: CandidatePlan) -> bool:
    """Return whether the declared output path already contains aggregation.

    ``MIN_GROUP_SIZE`` is a group-level privacy obligation. TrustAero IR v1 may
    guard an existing aggregate result, but it must not silently convert a
    detailed row-level query into a grouped query because that would change the
    user's task semantics without an explicit grouping contract.
    """

    by_id = {operator.operator_id: operator for operator in plan.operators}
    visited: set[str] = set()

    def visit(operator_id: str) -> bool:
        if operator_id in visited:
            return False
        visited.add(operator_id)
        operator = by_id.get(operator_id)
        if operator is None:
            return False
        if isinstance(operator, Aggregate):
            return True
        return any(visit(parent_id) for parent_id in operator.inputs)

    return visit(plan.output_operator)


@dataclass(frozen=True)
class RewriteOutcome:
    """Result of applying obligations without mutating the candidate plan."""

    operators: tuple[Operator, ...]
    output_operator: str
    reasons: tuple[ReasonCode, ...]
    error: Diagnostic | None = None


def _rewrite_obligations(
    plan: CandidatePlan, obligations: tuple[Obligation, ...]
) -> RewriteOutcome:
    """Apply deterministic, monotone obligation rewrites without mutating input.

    V1 only appends enforcers after the requested output. This intentionally
    avoids claiming unsafe pushdown before operator-specific proofs exist.
    """

    operators: list[Operator] = list(plan.operators)
    current_output = plan.output_operator
    reasons: list[ReasonCode] = []
    used_ids = {operator.operator_id for operator in operators}

    for index, obligation in enumerate(obligations, start=1):
        obligation_type = obligation.obligation_type
        if obligation_type == ObligationType.VERSION_PIN:
            # Snapshot bindings below satisfy a parameter-free pin obligation.
            # Version-specific parameters need explicit policy semantics first.
            if obligation.parameters:
                return RewriteOutcome(
                    tuple(operators),
                    current_output,
                    tuple(reasons),
                    _diagnostic(
                        ReasonCode.OBLIGATION_CONFLICT,
                        "Parameterized VERSION_PIN is not supported in IR v1.",
                        parameters=obligation.parameters,
                    ),
                )
            continue
        if obligation_type == ObligationType.MIN_GROUP_SIZE and not _output_depends_on_aggregate(
            plan
        ):
            return RewriteOutcome(
                tuple(operators),
                current_output,
                tuple(reasons),
                _diagnostic(
                    ReasonCode.OBLIGATION_CONFLICT,
                    "MIN_GROUP_SIZE requires an existing Aggregate on the output path in IR v1.",
                    obligation_type=obligation_type.value,
                ),
            )
        operator_type = {
            ObligationType.MASK: "Mask",
            ObligationType.GENERALIZE_LOCATION: "GeneralizeLocation",
            ObligationType.MIN_GROUP_SIZE: "MinGroupSize",
            ObligationType.LINEAGE_CAPTURE: "LineageCapture",
        }.get(obligation_type)
        if operator_type is None:
            # Ignoring an obligation would claim authorization without
            # enforcing its conditions, so unsupported semantics fail closed.
            return RewriteOutcome(
                tuple(operators),
                current_output,
                tuple(reasons),
                _diagnostic(
                    ReasonCode.OBLIGATION_CONFLICT,
                    "Policy obligation has no safe IR v1 implementation.",
                    obligation_type=obligation_type.value,
                ),
            )
        base_id = f"gov-{index:03d}-{operator_type.lower()}"
        operator_id = base_id
        collision_index = 1
        # Candidate IDs are untrusted. Pick a deterministic unused suffix so an
        # agent cannot collide with a governance operator by reserving its ID.
        while operator_id in used_ids:
            operator_id = f"{base_id}-{collision_index}"
            collision_index += 1
        params = obligation.parameters
        try:
            if obligation_type == ObligationType.MASK:
                new_operator: Operator = Mask(
                    operator_type="Mask",
                    operator_id=operator_id,
                    inputs=(current_output,),
                    fields=tuple(params["fields"]),
                    method=params.get("method", "redact"),
                )
                reasons.append(ReasonCode.MASK_REQUIRED)
            elif obligation_type == ObligationType.GENERALIZE_LOCATION:
                new_operator = GeneralizeLocation(
                    operator_type="GeneralizeLocation",
                    operator_id=operator_id,
                    inputs=(current_output,),
                    fields=tuple(params["fields"]),
                    precision_km=params["precision_km"],
                    method=params.get("method", "fixed_grid"),
                )
                reasons.append(ReasonCode.SPATIAL_PRECISION_EXCEEDED)
            elif obligation_type == ObligationType.MIN_GROUP_SIZE:
                new_operator = MinGroupSize(
                    operator_type="MinGroupSize",
                    operator_id=operator_id,
                    inputs=(current_output,),
                    minimum_count=params["minimum_count"],
                )
                reasons.append(ReasonCode.MIN_GROUP_SIZE_REQUIRED)
            else:
                new_operator = LineageCapture(
                    operator_type="LineageCapture",
                    operator_id=operator_id,
                    inputs=(current_output,),
                    level=params["level"],
                )
                reasons.append(ReasonCode.LINEAGE_REQUIRED)
        except (KeyError, TypeError, ValidationError) as exc:
            return RewriteOutcome(
                tuple(operators),
                current_output,
                tuple(reasons),
                _diagnostic(
                    ReasonCode.OBLIGATION_CONFLICT,
                    "Policy obligation parameters are invalid or incomplete.",
                    obligation_type=obligation_type.value,
                    error=str(exc),
                ),
            )
        operators.append(new_operator)
        used_ids.add(operator_id)
        current_output = operator_id
    return RewriteOutcome(tuple(operators), current_output, tuple(reasons))


def validate(
    raw_plan: dict[str, Any], policy_set: PolicySet, catalog: Catalog
) -> ValidatorResponse:
    """Validate a candidate plan deterministically and fail closed.

    The current milestone returns a validated logical plan. It never produces an
    executable physical plan; that permission boundary is reserved for a later
    optimizer/approval stage.
    """

    parsed = parse_candidate(raw_plan)
    if isinstance(parsed, ValidatorResponse):
        return parsed
    plan = parsed
    if plan.request_context.purpose is None:
        return ValidatorResponse(
            status=ValidationStatus.CLARIFY,
            candidate_plan_id=plan.plan_id,
            policy_decision=PolicyDecision.INDETERMINATE,
            diagnostics=(
                _diagnostic(ReasonCode.PURPOSE_MISSING, "A declared purpose is required."),
            ),
        )

    graph_diagnostics = validate_graph(plan)
    if graph_diagnostics:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            diagnostics=graph_diagnostics,
        )

    candidate_types = type_check_plan(plan, catalog)
    if candidate_types.diagnostics:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            diagnostics=candidate_types.diagnostics,
        )

    evaluation = evaluate_policy(plan, policy_set)
    if evaluation.decision == PolicyDecision.DENY:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=(_diagnostic(ReasonCode.POLICY_DENIED, evaluation.reason),),
        )
    if evaluation.decision == PolicyDecision.NOT_APPLICABLE:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=(_diagnostic(ReasonCode.POLICY_NOT_APPLICABLE, evaluation.reason),),
        )
    if evaluation.decision == PolicyDecision.INDETERMINATE:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=(_diagnostic(ReasonCode.POLICY_INDETERMINATE, evaluation.reason),),
        )

    normalization = normalize_obligations(
        evaluation.obligations,
        evaluation.obligation_sources,
    )
    if normalization.diagnostics:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=normalization.diagnostics,
        )
    normalized_obligations = normalization.normalized_obligations

    rewrite = _rewrite_obligations(plan, normalized_obligations)
    if rewrite.error is not None:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=(rewrite.error,),
        )
    operators = rewrite.operators
    rewrite_reasons = rewrite.reasons
    output_operator = rewrite.output_operator

    # Rewrites are not trusted merely because TrustAero produced them. Recheck
    # both graph invariants and schema transfer rules over the complete result.
    rewritten_plan = plan.model_copy(
        update={"operators": operators, "output_operator": output_operator}
    )
    rewritten_graph_diagnostics = validate_graph(rewritten_plan)
    if rewritten_graph_diagnostics:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=rewritten_graph_diagnostics,
        )

    rewritten_types = type_check_plan(rewritten_plan, catalog)
    if rewritten_types.diagnostics:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=rewritten_types.diagnostics,
        )

    data_snapshots: dict[str, str] = {}
    for operator in operators:
        if operator.operator_type != "ScanSource":
            continue
        dataset = catalog.get_dataset(operator.dataset)
        if dataset is None:  # Defensive fail-closed assertion after earlier resolution.
            return ValidatorResponse(
                status=ValidationStatus.REJECT,
                candidate_plan_id=plan.plan_id,
                diagnostics=(_diagnostic(ReasonCode.UNKNOWN_DATASET, operator.dataset),),
            )
        requested = operator.snapshot
        if requested is not None and requested not in dataset.versions:
            return ValidatorResponse(
                status=ValidationStatus.REJECT,
                candidate_plan_id=plan.plan_id,
                diagnostics=(
                    _diagnostic(
                        ReasonCode.VERSION_UNRESOLVED,
                        "Requested data snapshot is unavailable.",
                        dataset=operator.dataset,
                        snapshot=requested,
                    ),
                ),
            )
        data_snapshots[operator.dataset] = requested or dataset.default_version

    verification = verify_obligations(
        normalized_obligations,
        operators,
        boundary_operator=plan.output_operator,
        output_operator=output_operator,
        data_snapshots=data_snapshots,
    )
    if verification.diagnostics:
        return ValidatorResponse(
            status=ValidationStatus.REJECT,
            candidate_plan_id=plan.plan_id,
            policy_decision=evaluation.decision,
            diagnostics=verification.diagnostics,
        )

    digest = _canonical_digest(plan, operators, output_operator)
    logical_plan = ValidatedLogicalPlan(
        logical_plan_id="pl-" + digest.split(":", 1)[1][:16],
        candidate_plan_id=plan.plan_id,
        request_context=plan.request_context,
        requested_output=plan.requested_output,
        operators=operators,
        output_operator=output_operator,
        bindings=SnapshotBindings(
            policy_snapshot=policy_set.policy_snapshot,
            data_snapshots=data_snapshots,
        ),
        # Report only obligations actually enforced by an inserted operator or
        # by the snapshot binding logic above.
        satisfied_obligations=verification.satisfied,
        validation=ValidationSummary(
            rounds=2 if rewrite_reasons else 1,
            reason_codes=rewrite_reasons,
            canonical_digest=digest,
        ),
    )
    diagnostics = tuple(
        _diagnostic(code, "A governance obligation was inserted.") for code in rewrite_reasons
    )
    return ValidatorResponse(
        status=ValidationStatus.REWRITE if rewrite_reasons else ValidationStatus.ACCEPT,
        candidate_plan_id=plan.plan_id,
        policy_decision=PolicyDecision.PERMIT,
        diagnostics=diagnostics,
        validated_plan=logical_plan,
    )
