"""Repeatable Phase 0 validator micro-experiment runner."""

from __future__ import annotations

import copy
import csv
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from trustaero.experiments.loader import load_cases, load_catalog, load_json, load_policy
from trustaero.experiments.models import CaseResult, ExperimentCase, Phase0Config
from trustaero.ir.enums import LineageLevel, ValidationStatus
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    ExecutionEvent,
    GovernedExecutionCertificate,
    LineageEvidenceSummary,
    PhysicalOperatorSpec,
    ValidatedLogicalPlan,
)
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.hierarchical_planner import (
    GovernedCandidateProfile,
    hierarchical_planning_digest,
    plan_governed_candidates,
)
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.certificate import verify_execution_certificate
from trustaero.validator.service import validate

EventType = Literal[
    "PlanApproved",
    "OperatorStarted",
    "OperatorCompleted",
    "PolicyDecisionRecorded",
    "ResultMaterialized",
    "LineageRecorded",
    "CertificateEmitted",
]


@dataclass(frozen=True, slots=True)
class CertificateScenarioInputs:
    """Certificate inputs plus independently observed planner evidence."""

    physical_plan: ApprovedPhysicalPlan
    certificate: GovernedExecutionCertificate
    observed_planner_digest: str | None = None
    observed_planner_candidate_id: str | None = None
    planner_latency_ms: float = 0.0


def _repo_root() -> Path:
    """Return the repository root from this source file location."""

    return Path(__file__).resolve().parents[3]


def _git_commit(root: Path) -> str:
    """Record the exact source revision when git metadata is available."""

    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip()


def _run_id() -> str:
    """Use a sortable UTC run ID so result folders are easy to compare."""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _percentile_95(values: list[float]) -> float:
    """Return a simple nearest-rank P95 for short repeated measurements."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def _reason_codes(response: Any) -> tuple[str, ...]:
    """Extract sorted stable reason-code strings from a validator response."""

    return tuple(sorted({diagnostic.code.value for diagnostic in response.diagnostics}))


def _validation_reason_codes(response: Any) -> tuple[str, ...]:
    """Extract stable reason-code strings from validator diagnostics."""

    return _reason_codes(response)


def _operator_counts(plan: dict[str, Any]) -> tuple[int, int]:
    """Return logical operator and edge counts from a raw candidate plan."""

    operators = plan.get("operators", [])
    if not isinstance(operators, list):
        return 0, 0
    edge_count = 0
    for operator in operators:
        if isinstance(operator, dict) and isinstance(operator.get("inputs"), list):
            edge_count += len(operator["inputs"])
    return len(operators), edge_count


def _apply_validation_scenario(raw_plan: dict[str, Any], scenario: str) -> dict[str, Any]:
    """Apply a deterministic Phase 0 validation fault injection."""

    plan = copy.deepcopy(raw_plan)
    if scenario == "baseline":
        return plan
    if scenario == "unknown_dataset":
        plan["operators"][0]["dataset"] = "missing_dataset"
        return plan
    if scenario == "unknown_field":
        plan["operators"][-1]["fields"] = ["event_id", "missing_field"]
        return plan
    if scenario == "version_unresolved":
        plan["operators"][0]["snapshot"] = "v-missing"
        return plan
    if scenario == "unbound_reference":
        plan["operators"][-1]["inputs"] = ["missing-op"]
        return plan
    if scenario == "cyclic_plan":
        plan["operators"][0]["inputs"] = [plan["operators"][-1]["operator_id"]]
        return plan
    if scenario == "expression_type_mismatch":
        plan["plan_id"] = "p0-expression-type-mismatch"
        plan["requested_output"]["fields"] = ["event_id", "magnitude"]
        plan["operators"] = [
            {
                "operator_type": "ScanSource",
                "operator_id": "scan",
                "inputs": [],
                "dataset": "earthquakes",
                "snapshot": None,
            },
            {
                "operator_type": "Filter",
                "operator_id": "filter",
                "inputs": ["scan"],
                "expression": {
                    "expression_type": "comparison",
                    "operator": "eq",
                    "left": {"expression_type": "field", "field": "magnitude"},
                    "right": {
                        "expression_type": "literal",
                        "data_type": "boolean",
                        "value": True,
                    },
                },
            },
            {
                "operator_type": "Project",
                "operator_id": "project",
                "inputs": ["filter"],
                "fields": ["event_id", "magnitude"],
            },
        ]
        plan["output_operator"] = "project"
        return plan
    if scenario == "masked_filter":
        plan["plan_id"] = "p0-mask-filter"
        plan["requested_output"]["fields"] = ["event_id"]
        plan["operators"] = [
            {
                "operator_type": "ScanSource",
                "operator_id": "scan",
                "inputs": [],
                "dataset": "earthquakes",
                "snapshot": None,
            },
            {
                "operator_type": "Mask",
                "operator_id": "mask",
                "inputs": ["scan"],
                "fields": ["event_id"],
                "method": "hash",
            },
            {
                "operator_type": "Filter",
                "operator_id": "filter",
                "inputs": ["mask"],
                "expression": {
                    "expression_type": "comparison",
                    "operator": "eq",
                    "left": {"expression_type": "field", "field": "event_id"},
                    "right": {
                        "expression_type": "literal",
                        "data_type": "string",
                        "value": "evt-1",
                    },
                },
            },
            {
                "operator_type": "Project",
                "operator_id": "project",
                "inputs": ["filter"],
                "fields": ["event_id"],
            },
        ]
        plan["output_operator"] = "project"
        return plan
    raise ValueError(f"Unknown validation scenario: {scenario}")


def _event(
    sequence: int,
    event_type: EventType,
    payload_digest: str = "sha256:event",
    operator_id: str | None = None,
) -> ExecutionEvent:
    """Build an execution event for deterministic certificate scenarios."""

    return ExecutionEvent(
        sequence=sequence,
        event_type=event_type,
        operator_id=operator_id,
        payload_digest=payload_digest,
    )


def _events_for_physical_plan(physical: ApprovedPhysicalPlan) -> tuple[ExecutionEvent, ...]:
    """Build a complete structural event trace for an approved physical plan."""

    events: list[ExecutionEvent] = [_event(0, "PlanApproved", physical.physical_plan_id)]
    sequence = 1
    for operator in physical.physical_operators:
        events.append(
            _event(
                sequence,
                "OperatorStarted",
                f"sha256:start-{operator.physical_operator_id}",
                operator.physical_operator_id,
            )
        )
        sequence += 1
        events.append(
            _event(
                sequence,
                "OperatorCompleted",
                f"sha256:done-{operator.physical_operator_id}",
                operator.physical_operator_id,
            )
        )
        sequence += 1
    events.append(_event(sequence, "ResultMaterialized", "sha256:result"))
    sequence += 1
    events.append(_event(sequence, "LineageRecorded", "sha256:lineage"))
    sequence += 1
    events.append(_event(sequence, "CertificateEmitted", "sha256:certificate"))
    return tuple(events)


def _physical_plan_from_edges(
    plan: ValidatedLogicalPlan,
    edges: dict[str, tuple[str, ...]],
    output_operator: str,
) -> ApprovedPhysicalPlan:
    """Create a synthetic approved physical DAG for certificate fault injection."""

    return ApprovedPhysicalPlan(
        physical_plan_id="pp-phase0-synthetic",
        logical_plan_id=plan.logical_plan_id,
        logical_plan_digest=plan.validation.canonical_digest,
        output_operator=output_operator,
        physical_operators=tuple(
            PhysicalOperatorSpec(
                physical_operator_id=operator_id,
                logical_operator_id=f"logical-{operator_id}",
                operator_type="Synthetic",
                inputs=inputs,
            )
            for operator_id, inputs in edges.items()
        ),
        bindings=plan.bindings,
        lineage_instrumentation=plan.lineage_instrumentation,
        pending_obligations=plan.pending_obligations,
    )


def _certificate(
    plan: ValidatedLogicalPlan,
    physical: ApprovedPhysicalPlan,
) -> GovernedExecutionCertificate:
    """Build a structurally valid baseline certificate for Phase 0."""

    return GovernedExecutionCertificate(
        certificate_id="cert-phase0",
        task_digest=plan.validation.canonical_digest,
        logical_plan_id=plan.logical_plan_id,
        physical_plan_id=physical.physical_plan_id,
        policy_snapshot=plan.bindings.policy_snapshot,
        data_snapshots=plan.bindings.data_snapshots,
        events=_events_for_physical_plan(physical),
        result_digest="sha256:result",
        lineage_digest="sha256:lineage",
        lineage_evidence=LineageEvidenceSummary(
            execution_id="exec-phase0",
            result_id="result-phase0",
            lineage_level=LineageLevel.RECORD,
            covered_operators=(plan.lineage_requirements[0].target_operator,),
            edge_digest="sha256:edges",
        ),
    )


def _phase0_planner_decision() -> tuple[str, str]:
    """Build a deterministic real planner decision for fault injection."""

    profiles = (
        GovernedCandidateProfile(
            candidate_id="fused",
            result_equivalence_id="phase0-equivalent-result",
            exposure=CandidateExposure(
                "fused",
                0,
                0,
                provides_governance_checkpoint=False,
            ),
            work_metrics=(("pipeline_breaker.count", 0.0),),
        ),
        GovernedCandidateProfile(
            candidate_id="materialized",
            result_equivalence_id="phase0-equivalent-result",
            exposure=CandidateExposure("materialized", 0, 0),
            work_metrics=(("pipeline_breaker.count", 1.0),),
        ),
    )
    result = plan_governed_candidates(
        profiles,
        GovernanceFeasibilityPolicy("phase0-planner-policy", None, None),
    )
    if result.selected_candidate_id is None:
        raise ValueError("Phase 0 planner baseline must select one candidate")
    return hierarchical_planning_digest(result), result.selected_candidate_id


def _planner_certificate_inputs(
    plan: ValidatedLogicalPlan,
    scenario: str,
) -> CertificateScenarioInputs:
    """Create valid or deliberately corrupted planner/certificate bindings."""

    started = time.perf_counter()
    digest, selected = _phase0_planner_decision()
    planner_latency_ms = (time.perf_counter() - started) * 1000.0
    base = plan_physical_execution(plan)
    physical = base.model_copy(
        update={
            "planner_decision_digest": digest,
            "planner_selected_candidate_id": selected,
        }
    )
    certificate = _certificate(plan, physical).model_copy(
        update={
            "planner_decision_digest": digest,
            "planner_selected_candidate_id": selected,
        }
    )
    observed_digest = digest
    observed_candidate = selected

    if scenario == "planner_binding_valid":
        pass
    elif scenario == "planner_digest_mismatch":
        certificate = certificate.model_copy(update={"planner_decision_digest": "sha256:tampered"})
    elif scenario == "planner_candidate_mismatch":
        certificate = certificate.model_copy(
            update={"planner_selected_candidate_id": "materialized"}
        )
    elif scenario == "planner_strategy_mismatch":
        physical = physical.model_copy(update={"planner_selected_candidate_id": "materialized"})
        certificate = certificate.model_copy(
            update={"planner_selected_candidate_id": "materialized"}
        )
    elif scenario == "planner_binding_missing":
        certificate = _certificate(plan, physical)
    elif scenario == "planner_observation_mismatch":
        observed_digest = "sha256:independent-tamper"
    else:
        raise ValueError(f"Unknown planner certificate scenario: {scenario}")
    return CertificateScenarioInputs(
        physical_plan=physical,
        certificate=certificate,
        observed_planner_digest=observed_digest,
        observed_planner_candidate_id=observed_candidate,
        planner_latency_ms=planner_latency_ms,
    )


def _certificate_inputs(
    plan: ValidatedLogicalPlan,
    scenario: str,
) -> CertificateScenarioInputs:
    """Return physical plan and certificate for a deterministic certificate scenario."""

    if scenario.startswith("planner_"):
        return _planner_certificate_inputs(plan, scenario)
    physical = plan_physical_execution(plan)
    certificate = _certificate(plan, physical)
    if scenario == "baseline":
        return CertificateScenarioInputs(physical, certificate)
    if scenario == "weak_lineage":
        evidence = certificate.lineage_evidence
        if evidence is None:
            raise ValueError("weak_lineage scenario requires baseline lineage evidence")
        return CertificateScenarioInputs(
            physical,
            certificate.model_copy(
                update={
                    "lineage_evidence": evidence.model_copy(
                        update={"lineage_level": LineageLevel.SOURCE}
                    )
                }
            ),
        )
    if scenario == "missing_lineage_event":
        return CertificateScenarioInputs(
            physical,
            certificate.model_copy(
                update={
                    "events": tuple(
                        event
                        for event in certificate.events
                        if event.event_type != "LineageRecorded"
                    )
                }
            ),
        )
    if scenario == "snapshot_mismatch":
        return CertificateScenarioInputs(
            physical,
            certificate.model_copy(update={"data_snapshots": {"critical_facilities": "v1900"}}),
        )
    if scenario == "missing_result_digest":
        return CertificateScenarioInputs(
            physical,
            certificate.model_copy(update={"result_digest": ""}),
        )
    if scenario == "event_order_invalid":
        events = tuple(
            event.model_copy(update={"sequence": 1})
            if event.event_type == "ResultMaterialized"
            else event
            for event in certificate.events
        )
        return CertificateScenarioInputs(
            physical,
            certificate.model_copy(update={"events": events}),
        )
    if scenario == "unknown_physical_input":
        synthetic = _physical_plan_from_edges(
            plan,
            {"phys-filter": ("phys-missing",)},
            "phys-filter",
        )
        return CertificateScenarioInputs(synthetic, _certificate(plan, synthetic))
    if scenario == "cyclic_physical_plan":
        synthetic = _physical_plan_from_edges(
            plan,
            {
                "phys-a": ("phys-b",),
                "phys-b": ("phys-a",),
            },
            "phys-a",
        )
        return CertificateScenarioInputs(synthetic, _certificate(plan, synthetic))
    if scenario == "dependency_violation":
        synthetic = _physical_plan_from_edges(
            plan,
            {
                "phys-scan-a": (),
                "phys-scan-b": (),
                "phys-join": ("phys-scan-a", "phys-scan-b"),
            },
            "phys-join",
        )
        events = (
            _event(0, "PlanApproved", synthetic.physical_plan_id),
            _event(1, "OperatorStarted", "sha256:start-a", "phys-scan-a"),
            _event(2, "OperatorCompleted", "sha256:done-a", "phys-scan-a"),
            _event(3, "OperatorStarted", "sha256:start-b", "phys-scan-b"),
            _event(4, "OperatorStarted", "sha256:start-join", "phys-join"),
            _event(5, "OperatorCompleted", "sha256:done-b", "phys-scan-b"),
            _event(6, "OperatorCompleted", "sha256:done-join", "phys-join"),
            _event(7, "ResultMaterialized", "sha256:result"),
            _event(8, "LineageRecorded", "sha256:lineage"),
            _event(9, "CertificateEmitted", "sha256:certificate"),
        )
        return CertificateScenarioInputs(
            synthetic,
            _certificate(plan, synthetic).model_copy(update={"events": events}),
        )
    raise ValueError(f"Unknown certificate scenario: {scenario}")


def _diagnostic_payload(response: Any) -> list[dict[str, Any]]:
    """Serialize diagnostics for failure artifacts."""

    return [
        diagnostic.model_dump(mode="json") if hasattr(diagnostic, "model_dump") else {}
        for diagnostic in response.diagnostics
    ]


def _case_result(
    *,
    root: Path,
    run_id: str,
    commit_hash: str,
    case: ExperimentCase,
    warmup_runs: int,
    measured_runs: int,
) -> tuple[CaseResult, dict[str, Any] | None]:
    """Run one case with cold and preloaded core-validation measurements."""

    plan_path = root / case.plan_path
    policy_path = root / case.policy_path
    catalog_path = root / case.catalog_path

    # Cold timing includes file reads and typed policy/catalog loading. It is
    # useful for artifact users, but it is not the clean validator micro-cost.
    cold_start = time.perf_counter()
    raw_plan = load_json(plan_path)
    policy = load_policy(policy_path)
    catalog = load_catalog(catalog_path)
    if case.case_kind == "validation":
        raw_plan = _apply_validation_scenario(raw_plan, case.scenario)
    cold_response = validate(raw_plan, policy, catalog)
    certificate_event_count = 0
    if case.case_kind == "certificate":
        if cold_response.status != ValidationStatus.REWRITE or cold_response.validated_plan is None:
            raise ValueError(f"Certificate case {case.case_id} requires a rewrite validated plan")
        certificate_inputs = _certificate_inputs(
            cold_response.validated_plan,
            case.scenario,
        )
        physical = certificate_inputs.physical_plan
        certificate = certificate_inputs.certificate
        verification = verify_execution_certificate(
            cold_response.validated_plan,
            physical,
            certificate,
            observed_planner_decision_digest=(certificate_inputs.observed_planner_digest),
            observed_planner_selected_candidate_id=(
                certificate_inputs.observed_planner_candidate_id
            ),
        )
        certificate_event_count = len(certificate.events)
    cold_latency_ms = (time.perf_counter() - cold_start) * 1000.0

    for _ in range(warmup_runs):
        response = validate(raw_plan, policy, catalog)
        if case.case_kind == "certificate":
            if response.validated_plan is None:
                raise ValueError(f"Certificate case {case.case_id} did not rewrite in warmup")
            certificate_inputs = _certificate_inputs(
                response.validated_plan,
                case.scenario,
            )
            verify_execution_certificate(
                response.validated_plan,
                certificate_inputs.physical_plan,
                certificate_inputs.certificate,
                observed_planner_decision_digest=(certificate_inputs.observed_planner_digest),
                observed_planner_selected_candidate_id=(
                    certificate_inputs.observed_planner_candidate_id
                ),
            )

    measurements: list[float] = []
    planner_measurements: list[float] = []
    certificate_measurements: list[float] = []
    for _ in range(measured_runs):
        started = time.perf_counter()
        response = validate(raw_plan, policy, catalog)
        if case.case_kind == "certificate":
            if response.validated_plan is None:
                raise ValueError(f"Certificate case {case.case_id} did not rewrite")
            certificate_inputs = _certificate_inputs(
                response.validated_plan,
                case.scenario,
            )
            planner_measurements.append(certificate_inputs.planner_latency_ms)
            verification_started = time.perf_counter()
            verify_execution_certificate(
                response.validated_plan,
                certificate_inputs.physical_plan,
                certificate_inputs.certificate,
                observed_planner_decision_digest=(certificate_inputs.observed_planner_digest),
                observed_planner_selected_candidate_id=(
                    certificate_inputs.observed_planner_candidate_id
                ),
            )
            certificate_measurements.append((time.perf_counter() - verification_started) * 1000.0)
        measurements.append((time.perf_counter() - started) * 1000.0)

    if case.case_kind == "certificate":
        actual_status = verification.status.value
        actual_reason_codes = _reason_codes(verification)
        diagnostics_payload = _diagnostic_payload(verification)
    else:
        actual_status = cold_response.status.value
        actual_reason_codes = _validation_reason_codes(cold_response)
        diagnostics_payload = _diagnostic_payload(cold_response)
    expected_reason_codes = tuple(sorted(case.expected_reason_codes))
    status_correct = actual_status == case.expected_status
    reason_code_correct = set(expected_reason_codes).issubset(set(actual_reason_codes))
    if not expected_reason_codes:
        reason_code_correct = not actual_reason_codes

    operator_count, edge_count = _operator_counts(raw_plan)
    validated_plan = cold_response.validated_plan
    rewrite_rounds = None
    inserted_operator_count = 0
    pending_obligation_count = 0
    verified_obligation_count = 0
    plan_digest = ""
    if validated_plan is not None:
        rewrite_rounds = validated_plan.validation.rounds
        inserted_operator_count = max(0, len(validated_plan.operators) - operator_count)
        pending_obligation_count = len(validated_plan.pending_obligations)
        verified_obligation_count = len(validated_plan.satisfied_obligations)
        plan_digest = validated_plan.validation.canonical_digest

    result = CaseResult(
        run_id=run_id,
        commit_hash=commit_hash,
        case_id=case.case_id,
        case_category=case.case_category,
        case_kind=case.case_kind,
        scenario=case.scenario,
        expected_status=case.expected_status,
        actual_status=actual_status,
        status_correct=status_correct,
        expected_reason_codes=expected_reason_codes,
        actual_reason_codes=actual_reason_codes,
        reason_code_correct=reason_code_correct,
        runs=measured_runs,
        warmup_runs=warmup_runs,
        cold_latency_ms=cold_latency_ms,
        median_latency_ms=statistics.median(measurements),
        p95_latency_ms=_percentile_95(measurements),
        min_latency_ms=min(measurements),
        max_latency_ms=max(measurements),
        plan_size_bytes=plan_path.stat().st_size,
        operator_count=operator_count,
        edge_count=edge_count,
        rewrite_rounds=rewrite_rounds,
        inserted_operator_count=inserted_operator_count,
        pending_obligation_count=pending_obligation_count,
        verified_obligation_count=verified_obligation_count,
        certificate_event_count=certificate_event_count,
        planner_median_latency_ms=(
            statistics.median(planner_measurements) if planner_measurements else 0.0
        ),
        certificate_verification_median_latency_ms=(
            statistics.median(certificate_measurements) if certificate_measurements else 0.0
        ),
        plan_digest=plan_digest,
    )
    failure_payload = None
    if not status_correct or not reason_code_correct:
        failure_payload = {
            "case": asdict(case),
            "result": asdict(result),
            "diagnostics": diagnostics_payload,
            "input_paths": {
                "plan": str(plan_path),
                "policy": str(policy_path),
                "catalog": str(catalog_path),
            },
        }
    return result, failure_payload


def _write_cases_csv(path: Path, results: tuple[CaseResult, ...]) -> None:
    """Write stable-column per-case results."""

    fieldnames = list(asdict(results[0]).keys()) if results else list(CaseResult.__annotations__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["expected_reason_codes"] = "|".join(result.expected_reason_codes)
            row["actual_reason_codes"] = "|".join(result.actual_reason_codes)
            writer.writerow(row)


def _write_failure_artifacts(output_dir: Path, failures: dict[str, dict[str, Any]]) -> None:
    """Write one JSON artifact per failed case for easy debugging."""

    failure_dir = output_dir / "failures"
    failure_dir.mkdir(exist_ok=True)
    for case_id, payload in sorted(failures.items()):
        (failure_dir / f"{case_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _environment(root: Path, commit_hash: str) -> dict[str, object]:
    """Capture enough environment data to interpret Phase 0 numbers."""

    try:
        trustaero_version = metadata.version("trustaero")
    except metadata.PackageNotFoundError:
        trustaero_version = "editable-or-uninstalled"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "trustaero_version": trustaero_version,
        "commit_hash": commit_hash,
        "repo_root": str(root),
    }


def _summary(results: tuple[CaseResult, ...]) -> dict[str, object]:
    """Aggregate correctness and latency summaries for quick inspection."""

    total = len(results)
    status_correct = sum(result.status_correct for result in results)
    reason_correct = sum(result.reason_code_correct for result in results)
    failed = [
        result.case_id
        for result in results
        if not result.status_correct or not result.reason_code_correct
    ]
    medians = [result.median_latency_ms for result in results]
    planner_medians = [
        result.planner_median_latency_ms
        for result in results
        if result.planner_median_latency_ms > 0.0
    ]
    certificate_medians = [
        result.certificate_verification_median_latency_ms
        for result in results
        if result.certificate_verification_median_latency_ms > 0.0
    ]
    return {
        "case_count": total,
        "status_correct": status_correct,
        "reason_code_correct": reason_correct,
        "all_correct": not failed,
        "failed_cases": failed,
        "median_of_case_medians_ms": statistics.median(medians) if medians else 0.0,
        "max_case_p95_ms": max((result.p95_latency_ms for result in results), default=0.0),
        "median_planner_latency_ms": (
            statistics.median(planner_medians) if planner_medians else 0.0
        ),
        "median_certificate_verification_latency_ms": (
            statistics.median(certificate_medians) if certificate_medians else 0.0
        ),
    }


def run_phase0(config: Phase0Config, *, progress: bool = False) -> Path:
    """Run Phase 0 and return the created result directory."""

    root = _repo_root()
    run_id = _run_id()
    commit_hash = _git_commit(root)
    cases = load_cases(root / config.cases_path)
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=False)

    started = time.perf_counter()
    case_output_list: list[tuple[CaseResult, dict[str, Any] | None]] = []
    for index, case in enumerate(cases, start=1):
        case_output_list.append(
            _case_result(
                root=root,
                run_id=run_id,
                commit_hash=commit_hash,
                case=case,
                warmup_runs=config.warmup_runs,
                measured_runs=config.measured_runs,
            )
        )
        if progress:
            elapsed = time.perf_counter() - started
            eta = elapsed / index * (len(cases) - index)
            print(
                f"[Phase0 {index:02d}/{len(cases):02d}] {case.case_id} "
                f"{case.scenario} elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )
    case_outputs = tuple(case_output_list)
    results = tuple(result for result, _ in case_outputs)
    failures = {result.case_id: failure for result, failure in case_outputs if failure is not None}

    _write_cases_csv(output_dir / "cases.csv", results)
    _write_failure_artifacts(output_dir, failures)
    (output_dir / "summary.json").write_text(
        json.dumps(_summary(results), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "environment.json").write_text(
        json.dumps(_environment(root, commit_hash), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "config.json").write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_dir
