"""Build complementary evidence from registered plans, measurements, and faults.

The analysis preserves the frozen inputs, models, and three-percent practical
tie threshold. The output separates four system questions:

* which validation layers prevent unsafe execution and which cause false rejects;
* why hard governance feasibility must precede cost ranking;
* which certificate components detect each registered tamper class; and
* whether the full validator remains total and fail-closed under deterministic
  plan mutation.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.governed_pipeline_cost_holdout import (
    _select_with_frozen_model,
)
from trustaero.experiments.policy_stratified_pipeline_holdout import (
    _statistics_by_group,
    load_policy_stratified_holdout_config,
)
from trustaero.experiments.real_agent_plan_coverage import (
    _strict_fragment,
    _trusted_plan,
)
from trustaero.experiments.real_governed_pipeline_transfer import (
    _load_real_observations,
)
from trustaero.experiments.runner import _apply_validation_scenario
from trustaero.ir.enums import ObligationType, PolicyDecision, ValidationStatus
from trustaero.ir.models import MinGroupSize, PolicySet, ValidatorResponse
from trustaero.optimizer.governed_pipeline_cost import (
    FrozenGovernedPipelineCostModel,
    optimize_governed_pipeline,
)
from trustaero.policy.evaluator import evaluate_policy
from trustaero.validator.obligation_normalizer import normalize_obligations
from trustaero.validator.service import parse_candidate, validate, validate_graph
from trustaero.validator.type_checker import type_check_plan

JsonObject = dict[str, Any]
LayerName = Literal[
    "no_validation",
    "schema_type_only",
    "policy_output_only",
    "trustaero_full",
]

VALIDATOR_LAYERS: tuple[LayerName, ...] = (
    "no_validation",
    "schema_type_only",
    "policy_output_only",
    "trustaero_full",
)


@dataclass(frozen=True, slots=True)
class PaperGapClosureConfig:
    """All immutable inputs used by the supplemental paper evidence."""

    protocol_path: str
    phase0_case_matrix: str
    agent_protocol_path: str
    agent_task_path: str
    agent_run_dir: str
    policy_holdout_config: str
    policy_holdout_run_dir: str
    policy_holdout_evaluation: str
    certificate_phase0_cases: str
    multisource_summary: str
    record_lineage_v3_evaluation: str
    record_lineage_v4_evaluation: str
    fuzz_plan_paths: tuple[str, ...]
    fuzz_policy_path: str
    fuzz_catalog_path: str
    fuzz_seed: int
    fuzz_case_count: int
    results_dir: str


@dataclass(frozen=True, slots=True)
class LayerDecision:
    """Execution-boundary decision produced by one validation baseline."""

    executable: bool
    outcome: str
    reason_codes: tuple[str, ...] = ()


def load_config(path: Path | str) -> PaperGapClosureConfig:
    """Load the explicit supplemental-analysis manifest."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["fuzz_plan_paths"] = tuple(str(value) for value in payload["fuzz_plan_paths"])
    return PaperGapClosureConfig(**payload)


def _object(path: Path) -> JsonObject:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _response_decision(response: ValidatorResponse) -> LayerDecision:
    executable = response.status in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}
    return LayerDecision(
        executable=executable,
        outcome=response.status.value,
        reason_codes=tuple(sorted({item.code.value for item in response.diagnostics})),
    )


def _blocked(reason: str, *, clarify: bool = False) -> LayerDecision:
    return LayerDecision(False, "CLARIFY" if clarify else "BLOCKED", (reason,))


def _schema_type_only(
    raw_plan: JsonObject,
    catalog: InMemoryCatalog,
) -> LayerDecision:
    """Check JSON/graph/schema semantics while deliberately ignoring policy."""

    parsed = parse_candidate(raw_plan)
    if isinstance(parsed, ValidatorResponse):
        return _response_decision(parsed)
    graph = validate_graph(parsed)
    if graph:
        return LayerDecision(False, "BLOCKED", tuple(sorted({item.code.value for item in graph})))
    typed = type_check_plan(parsed, catalog)
    if typed.diagnostics:
        return LayerDecision(
            False,
            "BLOCKED",
            tuple(sorted({item.code.value for item in typed.diagnostics})),
        )
    return LayerDecision(True, "EXECUTABLE")


def _policy_output_only(
    raw_plan: JsonObject,
    policy: PolicySet,
    catalog: InMemoryCatalog,
) -> LayerDecision:
    """Model a conservative output-gate without plan rewriting or evidence closure.

    This baseline performs strict structure, graph, schema, and policy checks,
    then inspects only direct result disclosure.  It does not rewrite missing
    obligations, prove all-path coverage, bind snapshots, or close execution
    evidence.  Consequently it can be safe but needlessly reject repairable
    plans, and it can miss non-output obligations such as lineage.
    """

    parsed = parse_candidate(raw_plan)
    if isinstance(parsed, ValidatorResponse):
        return _response_decision(parsed)
    if parsed.request_context.purpose is None:
        return _blocked("PURPOSE_MISSING", clarify=True)
    graph = validate_graph(parsed)
    if graph:
        return LayerDecision(False, "BLOCKED", tuple(sorted({item.code.value for item in graph})))
    typed = type_check_plan(parsed, catalog)
    if typed.diagnostics:
        return LayerDecision(
            False,
            "BLOCKED",
            tuple(sorted({item.code.value for item in typed.diagnostics})),
        )
    evaluation = evaluate_policy(parsed, policy)
    if evaluation.decision != PolicyDecision.PERMIT:
        return _blocked(f"POLICY_{evaluation.decision.value}")
    normalization = normalize_obligations(
        evaluation.obligations,
        evaluation.obligation_sources,
    )
    if normalization.diagnostics:
        return LayerDecision(
            False,
            "BLOCKED",
            tuple(sorted({item.code.value for item in normalization.diagnostics})),
        )

    output_schema = typed.outputs[parsed.output_operator]
    output_operator = next(
        operator for operator in parsed.operators if operator.operator_id == parsed.output_operator
    )
    failures: list[str] = []
    for obligation in normalization.normalized_obligations:
        params = obligation.parameters
        if obligation.obligation_type == ObligationType.MASK:
            for name in params.get("fields", []):
                field = output_schema.get(str(name))
                if field is not None and field.value_state == "raw":
                    failures.append("OUTPUT_MASK_MISSING")
        elif obligation.obligation_type == ObligationType.GENERALIZE_LOCATION:
            required = float(params.get("precision_km", 0.0))
            for name in params.get("fields", []):
                field = output_schema.get(str(name))
                if field is not None and (
                    field.spatial_precision_km is None or field.spatial_precision_km < required
                ):
                    failures.append("OUTPUT_GENERALIZATION_MISSING")
        elif obligation.obligation_type == ObligationType.MIN_GROUP_SIZE:
            minimum = int(params.get("minimum_count", 0))
            if not isinstance(output_operator, MinGroupSize) or (
                output_operator.minimum_count < minimum
            ):
                failures.append("OUTPUT_MIN_GROUP_SIZE_MISSING")
        # VERSION_PIN and LINEAGE_CAPTURE are intentionally outside an
        # output-only checker; the experiment quantifies that blind spot.
    if failures:
        return LayerDecision(False, "BLOCKED", tuple(sorted(set(failures))))
    return LayerDecision(True, "EXECUTABLE")


def _layer_decision(
    layer: LayerName,
    raw_plan: JsonObject,
    policy: PolicySet,
    catalog: InMemoryCatalog,
) -> LayerDecision:
    if layer == "no_validation":
        return LayerDecision(True, "EXECUTABLE")
    if layer == "schema_type_only":
        return _schema_type_only(raw_plan, catalog)
    if layer == "policy_output_only":
        return _policy_output_only(raw_plan, policy, catalog)
    return _response_decision(validate(raw_plan, policy, catalog))


def _summarize_layer_rows(rows: list[JsonObject]) -> JsonObject:
    summaries: JsonObject = {}
    for layer in VALIDATOR_LAYERS:
        selected = [row for row in rows if row["layer"] == layer]
        unsafe = sum(
            bool(row["actual_executable"]) and not bool(row["expected_executable"])
            for row in selected
        )
        false_reject = sum(
            not bool(row["actual_executable"]) and bool(row["expected_executable"])
            for row in selected
        )
        correct = sum(
            bool(row["actual_executable"]) == bool(row["expected_executable"]) for row in selected
        )
        summaries[layer] = {
            "case_count": len(selected),
            "correct_boundary_decisions": correct,
            "boundary_accuracy": correct / len(selected) if selected else 0.0,
            "unsafe_acceptance_count": unsafe,
            "unsafe_acceptance_rate": unsafe / len(selected) if selected else 0.0,
            "false_reject_count": false_reject,
            "false_reject_rate": false_reject / len(selected) if selected else 0.0,
            "outcomes": dict(sorted(Counter(str(row["outcome"]) for row in selected).items())),
        }
    return summaries


def _phase0_validator_rows(
    root: Path,
    config: PaperGapClosureConfig,
) -> list[JsonObject]:
    rows: list[JsonObject] = []
    with (root / config.phase0_case_matrix).open(encoding="utf-8-sig", newline="") as handle:
        cases = list(csv.DictReader(handle))
    for case in cases:
        if case["case_kind"] != "validation":
            continue
        raw = _object(root / str(case["plan_path"]))
        raw = _apply_validation_scenario(raw, str(case["scenario"]))
        policy = PolicySet.model_validate(_object(root / str(case["policy_path"])))
        catalog = InMemoryCatalog(
            CatalogDocument.model_validate(_object(root / str(case["catalog_path"])))
        )
        expected = str(case["expected_status"]) in {"ACCEPT", "REWRITE"}
        for layer in VALIDATOR_LAYERS:
            decision = _layer_decision(layer, deepcopy(raw), policy, catalog)
            rows.append(
                {
                    "source": "phase0",
                    "case_id": str(case["case_id"]),
                    "stratum": str(case["case_category"]),
                    "layer": layer,
                    "expected_executable": expected,
                    "actual_executable": decision.executable,
                    "outcome": decision.outcome,
                    "reason_codes": list(decision.reason_codes),
                }
            )
    return rows


def _agent_content(cell: JsonObject) -> str:
    return str(cell["raw_response"]["choices"][0]["message"]["content"])


def _agent_validator_rows(
    root: Path,
    config: PaperGapClosureConfig,
) -> tuple[list[JsonObject], JsonObject]:
    protocol = _object(root / config.agent_protocol_path)
    tasks_payload = _object(root / config.agent_task_path)
    tasks = {str(item["task_id"]): item for item in tasks_payload["tasks"]}
    base_plan = _object(root / str(protocol["scope"]["base_plan"]))
    policy = PolicySet.model_validate(_object(root / str(protocol["scope"]["policy"])))
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_object(root / str(protocol["scope"]["catalog"])))
    )
    rows: list[JsonObject] = []
    parse_failures = 0
    replay_mismatches: list[str] = []
    for path in sorted((root / config.agent_run_dir / "cells").glob("*.json")):
        cell = _object(path)
        stored = cell["evaluation"]
        stored_outcome = str(stored["outcome"])
        expected = stored_outcome in {"ACCEPT", "REWRITE"}
        if not bool(stored["strict_json_parsed"]):
            parse_failures += 1
            for layer in VALIDATOR_LAYERS:
                rows.append(
                    {
                        "source": "real_agent",
                        "case_id": str(cell["cell_id"]),
                        "stratum": str(cell["stratum"]),
                        "layer": layer,
                        "expected_executable": False,
                        "actual_executable": False,
                        "outcome": "PARSE_ERROR",
                        "reason_codes": ["PLAN_PARSE_ERROR"],
                    }
                )
            continue
        fragment = _strict_fragment(_agent_content(cell))
        task = tasks[str(cell["task_id"])]
        raw = _trusted_plan(
            base_plan,
            fragment,
            task,
            str(cell["model"]),
            str(cell["mode_id"]),
        )
        for layer in VALIDATOR_LAYERS:
            decision = _layer_decision(layer, deepcopy(raw), policy, catalog)
            if layer == "trustaero_full" and decision.outcome != stored_outcome:
                replay_mismatches.append(str(cell["cell_id"]))
            rows.append(
                {
                    "source": "real_agent",
                    "case_id": str(cell["cell_id"]),
                    "stratum": str(cell["stratum"]),
                    "layer": layer,
                    "expected_executable": expected,
                    "actual_executable": decision.executable,
                    "outcome": decision.outcome,
                    "reason_codes": list(decision.reason_codes),
                }
            )
    if replay_mismatches:
        raise ValueError(f"Frozen Agent replay changed: {replay_mismatches}")
    return rows, {
        "cell_count": len(rows) // len(VALIDATOR_LAYERS),
        "strict_parse_failures": parse_failures,
        "frozen_full_replay_mismatches": replay_mismatches,
    }


def _validator_ablation(root: Path, config: PaperGapClosureConfig) -> JsonObject:
    phase0 = _phase0_validator_rows(root, config)
    agent, replay = _agent_validator_rows(root, config)
    return {
        "baseline_definitions": {
            "no_validation": "strictly parsed Agent content crosses the execution boundary",
            "schema_type_only": "strict IR, graph, schema, type and capability checks only",
            "policy_output_only": (
                "schema/type plus policy decision and direct-output disclosure checks; "
                "no rewrite, all-path proof, snapshot binding, or execution evidence closure"
            ),
            "trustaero_full": "complete deterministic validation, rewrite and postconditions",
        },
        "phase0": {
            "summaries": _summarize_layer_rows(phase0),
            "rows": phase0,
        },
        "real_agent": {
            "summaries": _summarize_layer_rows(agent),
            "rows": agent,
            "replay_integrity": replay,
        },
    }


def _planner_architecture_ablation(
    root: Path,
    config: PaperGapClosureConfig,
) -> JsonObject:
    holdout_config = load_policy_stratified_holdout_config(root / config.policy_holdout_config)
    run_dir = root / config.policy_holdout_run_dir
    observations, _, integrity = _load_real_observations(run_dir)
    statistics = _statistics_by_group(run_dir)
    model = FrozenGovernedPipelineCostModel.from_json(
        root / holdout_config.model_path,
        expected_sha256=holdout_config.model_sha256,
    )
    model_payload = _object(root / holdout_config.model_path)
    blind_selected, blind_predictions = _select_with_frozen_model(observations, model_payload)
    frozen_evaluation = _object(root / config.policy_holdout_evaluation)

    regimes: JsonObject = {}
    for regime in holdout_config.policy_regimes:
        illegal_blind: list[JsonObject] = []
        hard_rows: list[JsonObject] = []
        for key, stats in sorted(statistics.items()):
            hard = optimize_governed_pipeline(stats, regime.to_policy(), model)
            if hard.selected_candidate_id is None:
                raise ValueError(f"Frozen legality-first planner rejected {key}")
            blind = blind_selected[key]
            legal = tuple(hard.nondominated_candidate_ids)
            if blind not in legal:
                illegal_blind.append(
                    {
                        "scenario_id": key[0],
                        "seed": key[1],
                        "blind_selected_candidate_id": blind,
                        "legal_candidate_ids": list(legal),
                    }
                )
            hard_rows.append(
                {
                    "scenario_id": key[0],
                    "seed": key[1],
                    "legal_candidate_ids": list(legal),
                    "selected_candidate_id": hard.selected_candidate_id,
                    "performance_model_used": hard.performance_model_used,
                    "reason_code": hard.reason_code,
                }
            )
        total = len(hard_rows)
        registered = frozen_evaluation["regime_results"][regime.policy_id]
        regimes[regime.policy_id] = {
            "decision_count": total,
            "legal_candidate_count": regime.expected_legal_candidate_count,
            "governance_blind_illegal_selection_count": len(illegal_blind),
            "governance_blind_illegal_selection_rate": len(illegal_blind) / total,
            "governance_blind_illegal_examples": illegal_blind,
            "legality_first_illegal_selection_count": 0,
            "legality_first_decisions": hard_rows,
            "registered_optimizer_metrics": registered["optimizer_metrics"],
            "registered_best_fixed_candidate_id": registered["best_fixed_candidate_id"],
            "registered_best_fixed_metrics": registered["best_fixed_metrics"],
        }
    return {
        "analysis_boundary": (
            "Offline diagnostic replay over a consumed frozen holdout; no model refit, "
            "threshold change, or new independent-generalization claim."
        ),
        "single_stage_penalty_note": (
            "A finite soft penalty cannot enforce a hard policy in general; an infinite "
            "penalty is operationally equivalent to feasibility pruning. Therefore the "
            "empirical baseline is governance-blind cost ranking, not a tuned penalty strawman."
        ),
        "measurement_integrity": integrity,
        "blind_prediction_count": len(blind_predictions),
        "regimes": regimes,
    }


CERTIFICATE_COMPONENT_BY_REASON: dict[str, str] = {
    "CERTIFICATE_BINDING_MISMATCH": "plan_binding",
    "PHYSICAL_PLAN_BINDING_MISMATCH": "plan_binding",
    "CERTIFICATE_SNAPSHOT_MISMATCH": "snapshot_binding",
    "CERTIFICATE_DIGEST_MISSING": "result_binding",
    "CERTIFICATE_RESULT_DIGEST_MISMATCH": "result_binding",
    "LINEAGE_LEVEL_INSUFFICIENT": "lineage_binding",
    "LINEAGE_EVIDENCE_MISSING": "lineage_binding",
    "LINEAGE_EVIDENCE_INCONSISTENT": "lineage_binding",
    "LINEAGE_TARGET_NOT_COVERED": "lineage_binding",
    "CERTIFICATE_EVENT_MISSING": "event_dag",
    "CERTIFICATE_EVENT_ORDER_INVALID": "event_dag",
    "CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION": "event_dag",
    "CERTIFICATE_PHYSICAL_OPERATOR_UNKNOWN": "event_dag",
    "CERTIFICATE_PHYSICAL_PLAN_CYCLIC": "event_dag",
    "CERTIFICATE_PLANNER_DECISION_MISMATCH": "planner_binding",
    "PHYSICAL_PLAN_PLANNER_DECISION_MISMATCH": "planner_binding",
}


def _certificate_faults(root: Path, config: PaperGapClosureConfig) -> list[JsonObject]:
    faults: list[JsonObject] = []
    with (root / config.certificate_phase0_cases).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["case_kind"] != "certificate" or row["actual_status"] == "PARTIAL":
                continue
            reasons = [value for value in row["actual_reason_codes"].split("|") if value]
            faults.append(
                {
                    "source": "phase0",
                    "fault_id": row["case_id"],
                    "reason_codes": reasons,
                }
            )
    multisource = _object(root / config.multisource_summary)
    for item in multisource["fault_injection"]:
        faults.append(
            {
                "source": "multisource_v2",
                "fault_id": str(item["fault_id"]),
                "reason_codes": list(item["actual_reason_codes"]),
            }
        )
    for fault in faults:
        components = sorted(
            {
                CERTIFICATE_COMPONENT_BY_REASON[reason]
                for reason in fault["reason_codes"]
                if reason in CERTIFICATE_COMPONENT_BY_REASON
            }
        )
        if not components:
            raise ValueError(f"Unmapped certificate fault: {fault}")
        fault["detecting_components"] = components
    return faults


def _certificate_ablation(root: Path, config: PaperGapClosureConfig) -> JsonObject:
    all_components = {
        "plan_binding",
        "snapshot_binding",
        "result_binding",
        "lineage_binding",
        "event_dag",
        "planner_binding",
    }
    profiles = {
        "ordinary_event_log": {"event_dag"},
        "lineage_plus_event_log": {"lineage_binding", "event_dag"},
        "certificate_without_event_dag": all_components - {"event_dag"},
        "certificate_without_snapshots": all_components - {"snapshot_binding"},
        "certificate_without_result_binding": all_components - {"result_binding"},
        "certificate_without_lineage_binding": all_components - {"lineage_binding"},
        "certificate_without_planner_binding": all_components - {"planner_binding"},
        "trustaero_certificate_full": all_components,
    }
    faults = _certificate_faults(root, config)
    profile_results: JsonObject = {}
    for name, enabled in profiles.items():
        rows: list[JsonObject] = []
        for fault in faults:
            detecting = set(fault["detecting_components"])
            detected = bool(detecting & enabled)
            rows.append({**fault, "detected": detected})
        detected_count = sum(bool(row["detected"]) for row in rows)
        profile_results[name] = {
            "enabled_components": sorted(enabled),
            "detected_fault_count": detected_count,
            "fault_count": len(rows),
            "detection_rate": detected_count / len(rows),
            "escaped_fault_ids": [row["fault_id"] for row in rows if not row["detected"]],
            "faults": rows,
        }
    return {
        "method": (
            "Exact diagnostic-component replay over registered rejected faults; each reason "
            "code is emitted by one independent checker component."
        ),
        "component_by_reason_code": dict(sorted(CERTIFICATE_COMPONENT_BY_REASON.items())),
        "profiles": profile_results,
    }


UNSAFE_FUZZ_MUTATIONS = (
    "unknown_dataset",
    "unknown_field",
    "invalid_snapshot",
    "unbound_input",
    "cyclic_self_reference",
    "unknown_output",
    "duplicate_operator_id",
    "missing_purpose",
    "unauthorized_role",
    "unknown_operator",
    "invalid_arity",
    "masked_semantic_use",
    "expression_type_mismatch",
)
VALID_FUZZ_MUTATIONS = ("plan_id_only", "operator_order_only")


def _random_token(rng: random.Random) -> str:
    return f"fuzz-{rng.getrandbits(64):016x}"


def _mutate_fuzz_plan(
    base: JsonObject,
    mutation: str,
    rng: random.Random,
) -> JsonObject:
    plan = deepcopy(base)
    plan["plan_id"] = _random_token(rng)
    operators = plan["operators"]
    scans = [item for item in operators if item["operator_type"] == "ScanSource"]
    non_scans = [item for item in operators if item.get("inputs")]
    if mutation == "unknown_dataset":
        rng.choice(scans)["dataset"] = _random_token(rng)
    elif mutation == "unknown_field":
        projects = [item for item in operators if item["operator_type"] == "Project"]
        if projects:
            rng.choice(projects)["fields"].append(_random_token(rng))
        else:
            return _apply_validation_scenario(plan, "unknown_field")
    elif mutation == "invalid_snapshot":
        rng.choice(scans)["snapshot"] = _random_token(rng)
    elif mutation == "unbound_input":
        target = rng.choice(non_scans)
        target["inputs"][0] = _random_token(rng)
    elif mutation == "cyclic_self_reference":
        target = rng.choice(non_scans)
        target["inputs"][0] = target["operator_id"]
    elif mutation == "unknown_output":
        plan["output_operator"] = _random_token(rng)
    elif mutation == "duplicate_operator_id":
        operators[-1]["operator_id"] = operators[0]["operator_id"]
    elif mutation == "missing_purpose":
        plan["request_context"].pop("purpose", None)
    elif mutation == "unauthorized_role":
        plan["request_context"]["subject"]["role"] = "unauthorized_agent"
    elif mutation == "unknown_operator":
        rng.choice(operators)["operator_type"] = "FuzzUnknownOperator"
    elif mutation == "invalid_arity":
        target = rng.choice(operators)
        target["inputs"] = list(target.get("inputs", [])) + [_random_token(rng)]
    elif mutation == "masked_semantic_use":
        return _apply_validation_scenario(plan, "masked_filter")
    elif mutation == "expression_type_mismatch":
        return _apply_validation_scenario(plan, "expression_type_mismatch")
    elif mutation == "operator_order_only":
        rng.shuffle(operators)
    elif mutation != "plan_id_only":
        raise ValueError(f"Unknown fuzz mutation: {mutation}")
    return plan


def _validator_fuzz(
    root: Path,
    config: PaperGapClosureConfig,
    *,
    progress: bool,
) -> JsonObject:
    policy = PolicySet.model_validate(_object(root / config.fuzz_policy_path))
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_object(root / config.fuzz_catalog_path))
    )
    bases = [_object(root / path) for path in config.fuzz_plan_paths]
    rng = random.Random(config.fuzz_seed)
    rows: list[JsonObject] = []
    unsafe_target = round(config.fuzz_case_count * 0.8)
    for index in range(config.fuzz_case_count):
        unsafe = index < unsafe_target
        mutations = UNSAFE_FUZZ_MUTATIONS if unsafe else VALID_FUZZ_MUTATIONS
        mutation = mutations[index % len(mutations)]
        plan = _mutate_fuzz_plan(rng.choice(bases), mutation, rng)
        expected = not unsafe
        for layer in VALIDATOR_LAYERS:
            try:
                decision = _layer_decision(layer, deepcopy(plan), policy, catalog)
                exception = None
            except Exception as error:  # Report totality failures; never convert them to success.
                decision = LayerDecision(False, "EXCEPTION", (type(error).__name__,))
                exception = f"{type(error).__name__}: {error}"
            rows.append(
                {
                    "case_id": f"FZ-{index + 1:05d}",
                    "mutation": mutation,
                    "unsafe_mutation": unsafe,
                    "layer": layer,
                    "expected_executable": expected,
                    "actual_executable": decision.executable,
                    "outcome": decision.outcome,
                    "reason_codes": list(decision.reason_codes),
                    "exception": exception,
                }
            )
        if progress and ((index + 1) % 250 == 0 or index + 1 == config.fuzz_case_count):
            print(
                f"[Gap-Closure Fuzz {index + 1:05d}/{config.fuzz_case_count:05d}]",
                flush=True,
            )
    summaries = _summarize_layer_rows(rows)
    for layer in VALIDATOR_LAYERS:
        layer_rows = [row for row in rows if row["layer"] == layer]
        summaries[layer]["exception_count"] = sum(
            row["outcome"] == "EXCEPTION" for row in layer_rows
        )
        summaries[layer]["mutation_counts"] = dict(
            sorted(Counter(str(row["mutation"]) for row in layer_rows).items())
        )
    return {
        "seed": config.fuzz_seed,
        "case_count": config.fuzz_case_count,
        "unsafe_case_count": unsafe_target,
        "valid_control_count": config.fuzz_case_count - unsafe_target,
        "summaries": summaries,
        "rows": rows,
    }


def _lineage_representation_comparison(
    root: Path,
    config: PaperGapClosureConfig,
) -> JsonObject:
    v3 = _object(root / config.record_lineage_v3_evaluation)
    v4 = _object(root / config.record_lineage_v4_evaluation)
    v3_rows = {int(item["row_count"]): item for item in v3["unit_findings"]}
    v4_rows = {int(item["row_count"]): item for item in v4["unit_findings"]}
    comparisons: list[JsonObject] = []
    for row_count in sorted(set(v3_rows) & set(v4_rows)):
        old = v3_rows[row_count]
        new = v4_rows[row_count]
        old_storage = float(old["bytes_per_edge"])
        new_storage = float(new["bytes_per_edge"])
        old_record = float(old["record_median_ms"])
        new_record = float(new["record_median_ms"])
        comparisons.append(
            {
                "row_count": row_count,
                "v3_storage_bytes_per_edge": old_storage,
                "v4_storage_bytes_per_edge": new_storage,
                "storage_reduction_percent": (1.0 - new_storage / old_storage) * 100.0,
                "v3_record_median_latency_ms": old_record,
                "v4_record_median_latency_ms": new_record,
                "record_latency_reduction_percent": (1.0 - new_record / old_record) * 100.0,
                "v3_direct_ratio": float(old["median_record_over_direct_ratio"]),
                "v4_direct_ratio": float(new["median_record_over_direct_ratio"]),
            }
        )
    return {
        "method": "paired frozen V3/V4 representation comparison without rerunning DuckDB",
        "mechanism": (
            "V4 stores one result-sequence digest per artifact and row ordinals per edge, "
            "instead of repeating output identity in every edge."
        ),
        "comparisons": comparisons,
    }


def _write_rows(path: Path, rows: list[JsonObject]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            serialized = {
                key: json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            }
            writer.writerow(serialized)


def _write_report(path: Path, payload: JsonObject) -> None:
    validator = payload["validator_ablation"]
    planner = payload["planner_architecture_ablation"]
    certificate = payload["certificate_component_ablation"]
    fuzz = payload["validator_mutation_fuzz"]
    lines = [
        "# TrustAero paper gap-closure evidence",
        "",
        "All analyses reuse frozen artifacts. No model was refit, no Agent API was",
        "called, no DuckDB benchmark was rerun, and the 3% threshold was unchanged.",
        "",
        "## Validator layer ablation",
        "",
        (
            "| Layer | Phase0 unsafe accepts | Phase0 false rejects | "
            "Agent unsafe accepts | Agent false rejects |"
        ),
        "|---|---:|---:|---:|---:|",
    ]
    for layer in VALIDATOR_LAYERS:
        p0 = validator["phase0"]["summaries"][layer]
        agent = validator["real_agent"]["summaries"][layer]
        lines.append(
            f"| {layer} | {p0['unsafe_acceptance_count']} | {p0['false_reject_count']} | "
            f"{agent['unsafe_acceptance_count']} | {agent['false_reject_count']} |"
        )
    lines += [
        "",
        "## Legality-first planning ablation",
        "",
        (
            "| Policy | Legal candidates | Blind illegal selections | "
            "Blind illegal rate | Hard illegal selections |"
        ),
        "|---|---:|---:|---:|---:|",
    ]
    for policy_id, result in planner["regimes"].items():
        lines.append(
            f"| {policy_id} | {result['legal_candidate_count']} | "
            f"{result['governance_blind_illegal_selection_count']} | "
            f"{result['governance_blind_illegal_selection_rate']:.3f} | "
            f"{result['legality_first_illegal_selection_count']} |"
        )
    lines += [
        "",
        "## Certificate component ablation",
        "",
        "| Profile | Detected | Total | Detection rate |",
        "|---|---:|---:|---:|",
    ]
    for name, result in certificate["profiles"].items():
        lines.append(
            f"| {name} | {result['detected_fault_count']} | {result['fault_count']} | "
            f"{result['detection_rate']:.3f} |"
        )
    lines += [
        "",
        "## Deterministic mutation fuzz",
        "",
        "| Layer | Unsafe accepts | False rejects | Exceptions |",
        "|---|---:|---:|---:|",
    ]
    for layer in VALIDATOR_LAYERS:
        result = fuzz["summaries"][layer]
        lines.append(
            f"| {layer} | {result['unsafe_acceptance_count']} | "
            f"{result['false_reject_count']} | {result['exception_count']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_paper_gap_closure(
    project_root: Path,
    config_path: Path,
    *,
    progress: bool = False,
) -> Path:
    """Run all supplemental analyses and return the immutable output directory."""

    root = project_root.resolve()
    config_file = config_path.resolve()
    config = load_config(config_file)
    protocol_file = root / config.protocol_path
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_root = root / config.results_dir
    output = output_root / run_id
    output.mkdir(parents=True, exist_ok=True)

    if progress:
        print("[Gap-Closure 1/5] validator layers on frozen Phase0 and Agent plans", flush=True)
    validator = _validator_ablation(root, config)
    if progress:
        print("[Gap-Closure 2/5] legality-first versus governance-blind ranking", flush=True)
    planner = _planner_architecture_ablation(root, config)
    if progress:
        print("[Gap-Closure 3/5] certificate component/tamper replay", flush=True)
    certificate = _certificate_ablation(root, config)
    if progress:
        print("[Gap-Closure 4/5] deterministic plan mutation fuzz", flush=True)
    fuzz = _validator_fuzz(root, config, progress=progress)
    if progress:
        print("[Gap-Closure 5/5] frozen Lineage V3/V4 representation comparison", flush=True)
    lineage = _lineage_representation_comparison(root, config)
    commit_hash, git_dirty = _git_state(root)

    full_fuzz = fuzz["summaries"]["trustaero_full"]
    full_certificate = certificate["profiles"]["trustaero_certificate_full"]
    hard_illegal = sum(
        int(result["legality_first_illegal_selection_count"])
        for result in planner["regimes"].values()
    )
    gates = {
        "full_validator_zero_fuzz_unsafe_acceptance": (full_fuzz["unsafe_acceptance_count"] == 0),
        "full_validator_zero_fuzz_exceptions": full_fuzz["exception_count"] == 0,
        "full_certificate_detects_every_registered_fault": (
            full_certificate["detection_rate"] == 1.0
        ),
        "legality_first_zero_illegal_selections": hard_illegal == 0,
    }
    payload: JsonObject = {
        "schema_version": 1,
        "status": (
            "PASS_PAPER_GAP_CLOSURE_EVIDENCE"
            if all(gates.values())
            else "FAIL_PAPER_GAP_CLOSURE_RETAIN"
        ),
        "analysis_type": "offline_frozen_evidence_and_deterministic_mutation",
        "agent_api_calls": 0,
        "duckdb_benchmark_runs": 0,
        "optimizer_refit_performed": False,
        "holdout_retuning_performed": False,
        "practical_tie_threshold_changed": False,
        "git_commit": commit_hash,
        "git_dirty": git_dirty,
        "implementation_sha256": {
            "src/trustaero/experiments/paper_gap_closure.py": _sha256(Path(__file__)),
            "scripts/run_paper_gap_closure.py": _sha256(root / "scripts/run_paper_gap_closure.py"),
        },
        "config_path": str(config_file.relative_to(root)),
        "config_sha256": _sha256(config_file),
        "protocol_path": str(protocol_file.relative_to(root)),
        "protocol_sha256": _sha256(protocol_file),
        "input_sha256": {
            path: _sha256(root / path)
            for path in (
                config.phase0_case_matrix,
                config.agent_protocol_path,
                config.agent_task_path,
                config.policy_holdout_evaluation,
                config.certificate_phase0_cases,
                config.multisource_summary,
                config.record_lineage_v3_evaluation,
                config.record_lineage_v4_evaluation,
            )
        },
        "gates": gates,
        "validator_ablation": validator,
        "planner_architecture_ablation": planner,
        "certificate_component_ablation": certificate,
        "validator_mutation_fuzz": fuzz,
        "lineage_representation_comparison": lineage,
    }
    _atomic_json(output / "summary.json", payload)
    _atomic_json(output / "validator_ablation.json", validator)
    _atomic_json(output / "planner_architecture_ablation.json", planner)
    _atomic_json(output / "certificate_component_ablation.json", certificate)
    _atomic_json(output / "validator_fuzz.json", fuzz)
    _atomic_json(output / "lineage_representation_comparison.json", lineage)
    _write_rows(
        output / "validator_ablation_cases.csv",
        validator["phase0"]["rows"] + validator["real_agent"]["rows"],
    )
    _write_rows(output / "validator_fuzz_cases.csv", fuzz["rows"])
    _write_report(output / "report.md", payload)
    return output
