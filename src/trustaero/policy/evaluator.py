"""Small deterministic policy evaluator for the V1 policy fragment.

This module borrows the decision vocabulary from XACML, but it does not claim
XACML conformance. NOT_APPLICABLE is never interpreted as permission.
"""

from dataclasses import dataclass

from trustaero.ir.enums import PolicyDecision
from trustaero.ir.models import CandidatePlan, Obligation, PolicyRule, PolicySet


@dataclass(frozen=True)
class Evaluation:
    decision: PolicyDecision
    obligations: tuple[Obligation, ...]
    matched_rules: tuple[PolicyRule, ...]
    reason: str


def evaluate_policy(plan: CandidatePlan, policy_set: PolicySet) -> Evaluation:
    """Evaluate applicable rules with deterministic deny-overrides semantics."""

    purpose = plan.request_context.purpose
    if purpose is None:
        return Evaluation(PolicyDecision.INDETERMINATE, (), (), "purpose is missing")

    resources = {
        operator.dataset for operator in plan.operators if operator.operator_type == "ScanSource"
    }
    matched = tuple(
        rule
        for rule in policy_set.rules
        if plan.request_context.subject.role in rule.subject_roles
        and purpose in rule.purposes
        and plan.request_context.action in rule.actions
        and resources.issubset(set(rule.resources))
    )
    if not matched:
        return Evaluation(PolicyDecision.NOT_APPLICABLE, (), (), "no applicable permit rule")
    if any(rule.decision == PolicyDecision.DENY for rule in matched):
        return Evaluation(PolicyDecision.DENY, (), matched, "deny-overrides")
    if any(rule.decision == PolicyDecision.INDETERMINATE for rule in matched):
        return Evaluation(PolicyDecision.INDETERMINATE, (), matched, "indeterminate rule")

    obligations: list[Obligation] = []
    seen: set[tuple[str, str]] = set()
    for rule in matched:
        for obligation in rule.obligations:
            key = (obligation.obligation_type.value, repr(sorted(obligation.parameters.items())))
            if key not in seen:
                obligations.append(obligation)
                seen.add(key)
    return Evaluation(PolicyDecision.PERMIT, tuple(obligations), matched, "permit")
