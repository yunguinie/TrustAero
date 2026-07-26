"""Complete four-source TrustAero semantic loop with planner binding.

V1 proved that one governed four-source query could be rewritten, executed,
lineage-checked, and certified.  V2 closes the remaining system-integration
gaps without turning the case study into a performance benchmark:

* one safe and one policy-inapplicable Agent request are validated;
* a bounded physical candidate set is generated from the validated Pl;
* hard governance feasibility is evaluated before any ranking;
* the selected candidate and complete planning trace are bound into Pp and
  the execution certificate;
* policy and planner-decision faults are added to the V1 fault suite.

Four-source record lineage is deliberately not fabricated.  The separate
ordinal Lineage V4 implementation supports one identity-preserving source,
whereas this query contains three many-to-many spatial joins.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data.download import sha256_file
from trustaero.execution import (
    capture_source_lineage,
    compile_approved_physical_plan,
    execute_with_connection,
)
from trustaero.experiments.multisource_case_study import (
    MultisourceCaseStudyError,
    _atomic_json,
    _certificate_events,
    _certificate_fault_injection,
    _create_trusted_views,
    _diagnostic_codes,
    _git_state,
    _load_json,
    _require_rejected_fault,
    verify_multisource_case_artifacts,
)
from trustaero.ir.enums import ObligationType, ReasonCode, ValidationStatus
from trustaero.ir.models import GovernedExecutionCertificate, PolicySet
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
)
from trustaero.optimizer.hierarchical_planner import (
    GovernedCandidateProfile,
    HierarchicalPlannerConfig,
    hierarchical_planning_digest,
    plan_governed_candidates,
)
from trustaero.planner.candidates import generate_duckdb_candidates
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)
from trustaero.validator.service import validate

_PROTOCOL = Path("experiments/frozen/multisource_case_study_v2_protocol_20260726.json")
_RESULTS = Path("results/multisource_case_study_v2")
_EXPECTED_SAFE_REASONS = {
    ReasonCode.MASK_REQUIRED.value,
    ReasonCode.LINEAGE_REQUIRED.value,
}


def _agent_plans(
    project_root: Path, request_spec: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct both frozen Agent requests without trusting either request."""

    safe_spec = request_spec["safe_request"]
    illegal_spec = request_spec["illegal_request"]
    safe = _load_json(project_root / str(safe_spec["base_plan_path"]))
    illegal = json.loads(json.dumps(safe))
    mutation = illegal_spec["mutation"]
    if mutation["json_path"] != "request_context.subject.role":
        raise MultisourceCaseStudyError("Unsupported frozen Agent mutation")
    illegal["request_context"]["subject"]["role"] = str(mutation["value"])
    return safe, illegal


def _candidate_profiles(
    candidates: tuple[Any, ...],
    artifact_verification: dict[str, Any],
) -> tuple[GovernedCandidateProfile, ...]:
    """Derive conservative exposure bounds from trusted plan construction.

    The policy limit for raw materialization is zero, so a sound positive upper
    bound is sufficient to reject a raw checkpoint.  The bounds below come
    from the audited source row counts and are never supplied by the Agent.
    """

    rows = {
        str(item["artifact_id"]): int(item["row_count"])
        for item in artifact_verification["outputs"]
    }
    earthquake_rows = rows["multisource_earthquakes_v1"]
    well_rows = rows["multisource_wells_v1"]
    all_source_product = 1
    for value in rows.values():
        all_source_product *= value

    raw_bounds = {
        "fused": 0,
        "materialize-after-earthquake-magnitude": earthquake_rows,
        "materialize-after-earthquake-well-join": earthquake_rows * well_rows,
        "materialize-after-gov-001-mask": 0,
    }
    masked_bounds = {
        "fused": 0,
        "materialize-after-earthquake-magnitude": 0,
        "materialize-after-earthquake-well-join": 0,
        "materialize-after-gov-001-mask": all_source_product,
    }
    profiles: list[GovernedCandidateProfile] = []
    for candidate in candidates:
        candidate_id = candidate.strategy.strategy_id
        if candidate_id not in raw_bounds:
            raise MultisourceCaseStudyError(f"Unexpected physical candidate: {candidate_id}")
        is_checkpoint = candidate_id != "fused"
        exposure = CandidateExposure(
            candidate_id=candidate_id,
            raw_rows_exposed_to_join=0,
            raw_rows_materialized=raw_bounds[candidate_id],
            masked_rows_materialized=masked_bounds[candidate_id],
            provides_governance_checkpoint=is_checkpoint,
        )
        profiles.append(
            GovernedCandidateProfile(
                candidate_id=candidate_id,
                result_equivalence_id="multisource-governed-result-v2",
                exposure=exposure,
                work_metrics=(
                    ("materialization.boundaries", float(is_checkpoint)),
                    (
                        "materialization.masked_rows_upper_bound",
                        float(masked_bounds[candidate_id]),
                    ),
                    (
                        "materialization.raw_rows_upper_bound",
                        float(raw_bounds[candidate_id]),
                    ),
                ),
            )
        )
    return tuple(profiles)


def _policy_faults(
    *,
    plan: Any,
    physical: Any,
    certificate: GovernedExecutionCertificate,
    observed_result_digest: str,
    planner_digest: str,
    selected_candidate_id: str,
) -> list[dict[str, Any]]:
    """Add policy and optimizer-binding faults to the V1 certificate suite."""

    common = {
        "observed_result_digest": observed_result_digest,
        "observed_planner_decision_digest": planner_digest,
        "observed_planner_selected_candidate_id": selected_candidate_id,
    }
    policy_check = verify_execution_certificate(
        plan,
        physical,
        certificate.model_copy(update={"policy_snapshot": "tampered-policy-snapshot"}),
        **common,
    )
    planner_check = verify_execution_certificate(
        plan,
        physical,
        certificate.model_copy(update={"planner_decision_digest": "sha256:" + "0" * 64}),
        **common,
    )
    return [
        _require_rejected_fault(
            fault_id="policy_snapshot_tamper",
            check=policy_check,
            expected_code=ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH,
        ),
        _require_rejected_fault(
            fault_id="planner_decision_tamper",
            check=planner_check,
            expected_code=ReasonCode.CERTIFICATE_PLANNER_DECISION_MISMATCH,
        ),
    ]


def _report_markdown(summary: dict[str, Any]) -> str:
    """Render a small human-readable report beside the machine evidence."""

    planning = summary["candidate_planning"]
    faults = summary["fault_injection"]
    return "\n".join(
        [
            "# TrustAero four-source end-to-end case study V2",
            "",
            f"- Status: `{summary['status']}`",
            f"- Valid Agent request: `{summary['agent_validation']['safe_status']}`",
            f"- Illegal Agent request: `{summary['agent_validation']['illegal_status']}`",
            f"- Validated logical plan (Pl): `{summary['validation']['logical_plan_id']}`",
            f"- Generated physical candidates: `{planning['generated_candidate_count']}`",
            f"- Policy-rejected candidates: `{len(planning['rejected_candidate_ids'])}`",
            f"- Optimizer-selected candidate: `{planning['selected_candidate_id']}`",
            f"- Approved physical plan (Pp): `{summary['physical_plan']['physical_plan_id']}`",
            f"- Governed output rows: `{summary['execution']['row_count']}`",
            f"- Source-lineage inputs: `{summary['lineage']['source_count']}`",
            f"- Certificate status: `{summary['certificate']['status']}`",
            f"- Rejected injected faults: `{len(faults)}/{len(faults)}`",
            "",
            "## Reproduce",
            "",
            "```powershell",
            "conda activate TrustAero_env",
            "python -u scripts/download_datasets.py --stage multisource_case_v1",
            "python -u scripts/prepare_multisource_case.py --progress",
            "python -u scripts/run_multisource_case_study_v2.py --progress --require-clean",
            "```",
            "",
            "This is semantic system evidence, not an optimizer speed benchmark. "
            "Certificate `PARTIAL` is intentional because DuckDB-internal operator "
            "outputs are not cryptographically attested.",
            "",
        ]
    )


def run_multisource_case_study_v2(
    project_root: Path,
    *,
    progress: bool = False,
    require_clean: bool = False,
) -> Path:
    """Run the frozen complete-system loop and persist auditable evidence."""

    project_root = project_root.resolve()
    commit, dirty = _git_state(project_root)
    if require_clean and dirty:
        raise MultisourceCaseStudyError(
            "Publication evidence requires a clean Git tree; commit V2 before running."
        )

    def report(step: int, total: int, message: str) -> None:
        if progress:
            print(f"[Multisource-V2 {step:02d}/{total:02d}] {message}", flush=True)

    protocol = _load_json(project_root / _PROTOCOL)
    report(1, 9, "verifying four frozen sources and constructing Agent requests")
    artifacts = verify_multisource_case_artifacts(project_root)
    requests = _load_json(project_root / str(protocol["agent_requests_path"]))
    safe_request, illegal_request = _agent_plans(project_root, requests)
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(project_root / str(protocol["catalog_path"])))
    )
    policy = PolicySet.model_validate(_load_json(project_root / str(protocol["policy_path"])))

    report(2, 9, "validating safe and illegal Agent requests")
    safe_response = validate(safe_request, policy, catalog)
    illegal_response = validate(illegal_request, policy, catalog)
    safe_reasons = {item.code.value for item in safe_response.diagnostics}
    illegal_reasons = {item.code.value for item in illegal_response.diagnostics}
    expected_illegal = set(requests["illegal_request"]["expected_reason_codes"])
    if (
        safe_response.status != ValidationStatus.REWRITE
        or safe_response.validated_plan is None
        or safe_reasons != _EXPECTED_SAFE_REASONS
    ):
        raise MultisourceCaseStudyError("Safe Agent request did not produce the frozen Pl")
    if (
        illegal_response.status != ValidationStatus.REJECT
        or illegal_response.validated_plan is not None
        or illegal_reasons != expected_illegal
    ):
        raise MultisourceCaseStudyError("Illegal Agent request was not rejected fail-closed")
    plan = safe_response.validated_plan

    report(3, 9, "generating bounded physical candidates")
    targets = tuple(protocol["physical_candidates"]["materialization_targets"])
    candidates = generate_duckdb_candidates(plan, materialization_targets=targets)
    candidate_ids = tuple(candidate.strategy.strategy_id for candidate in candidates)
    if candidate_ids != tuple(protocol["physical_candidates"]["expected_strategy_ids"]):
        raise MultisourceCaseStudyError("Generated physical candidate space changed")

    report(4, 9, "filtering governance violations before optimizer selection")
    physical_policy = protocol["physical_governance_policy"]
    feasibility_policy = GovernanceFeasibilityPolicy(
        policy_id=str(physical_policy["policy_id"]),
        max_raw_join_rows=physical_policy["max_raw_join_rows"],
        max_raw_materialized_rows=physical_policy["max_raw_materialized_rows"],
        require_governance_checkpoint=bool(physical_policy["require_governance_checkpoint"]),
    )
    profiles = _candidate_profiles(candidates, artifacts)
    required_candidate = str(protocol["required_selected_strategy"])
    planning = plan_governed_candidates(
        profiles,
        feasibility_policy,
        HierarchicalPlannerConfig(conservative_fallback_candidate_id=required_candidate),
    )
    if planning.status != "SELECT" or planning.selected_candidate_id != required_candidate:
        raise MultisourceCaseStudyError("Governance-first optimizer selected the wrong candidate")
    planner_digest = hierarchical_planning_digest(planning)
    physical_by_id = {candidate.strategy.strategy_id: candidate for candidate in candidates}
    physical = physical_by_id[required_candidate].model_copy(
        update={
            "planner_decision_digest": planner_digest,
            "planner_selected_candidate_id": required_candidate,
        }
    )

    report(5, 9, "executing selected Pp twice and checking governed output")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise MultisourceCaseStudyError("DuckDB is required inside TrustAero_env") from exc
    connection = duckdb.connect()
    try:
        bindings = _create_trusted_views(connection, project_root, artifacts)
        compiled = compile_approved_physical_plan(plan, physical, catalog, bindings)
        first = execute_with_connection(compiled, connection)
        second = execute_with_connection(compiled, connection)
        if (
            first.row_count <= 0
            or first.row_count != second.row_count
            or first.result_digest != second.result_digest
            or first.result_digest != protocol["required_result_digest"]
        ):
            raise MultisourceCaseStudyError("Selected Pp changed the frozen governed result")
        sensitive_index = first.columns.index("api_well_number")
        raw_values = {
            str(row[0])
            for row in connection.execute(
                "SELECT api_well_number FROM trust_wells WHERE api_well_number IS NOT NULL"
            ).fetchall()
        }
        masked_values = [
            str(row[sensitive_index]) for row in first.rows if row[sensitive_index] is not None
        ]
        raw_exposure_rows = sum(value in raw_values for value in masked_values)
        malformed_masks = sum(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in masked_values
        )
        if not masked_values or raw_exposure_rows or malformed_masks:
            raise MultisourceCaseStudyError("Selected Pp exposed malformed or raw identifiers")
    finally:
        connection.close()

    report(6, 9, "capturing four-source lineage and binding planner decision")
    execution_id = "exec-multisource-case-v2"
    lineage = capture_source_lineage(
        plan,
        execution_id=execution_id,
        result_id=first.result_digest,
    )
    if lineage.evidence is None or lineage.lineage_digest is None or lineage.source_count != 4:
        raise MultisourceCaseStudyError("Source lineage did not cover all four scans")
    certificate = GovernedExecutionCertificate(
        certificate_id="cert-multisource-case-v2",
        task_digest=plan.validation.canonical_digest,
        logical_plan_id=plan.logical_plan_id,
        physical_plan_id=physical.physical_plan_id,
        policy_snapshot=plan.bindings.policy_snapshot,
        data_snapshots=plan.bindings.data_snapshots,
        events=_certificate_events(
            physical,
            policy_snapshot=plan.bindings.policy_snapshot,
            result_digest=first.result_digest,
            lineage_digest=lineage.lineage_digest,
        ),
        result_digest=first.result_digest,
        lineage_evidence=lineage.evidence,
        lineage_digest=lineage.lineage_digest,
        planner_decision_digest=planner_digest,
        planner_selected_candidate_id=required_candidate,
    )

    report(7, 9, "independently checking plan, planner, snapshots, result, events, and lineage")
    certificate_check = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest=first.result_digest,
        observed_planner_decision_digest=planner_digest,
        observed_planner_selected_candidate_id=required_candidate,
    )
    if (
        certificate_check.status != CertificateVerificationStatus.PARTIAL
        or certificate_check.diagnostics
        or ObligationType.MASK not in certificate_check.verified_obligations
        or ObligationType.LINEAGE_CAPTURE not in certificate_check.verified_obligations
        or tuple(certificate_check.unverified_components) != ("physical_plan_execution",)
    ):
        raise MultisourceCaseStudyError(
            f"Independent V2 certificate check failed: {_diagnostic_codes(certificate_check)}"
        )

    report(8, 9, "injecting result, policy, data, lineage, event, and planner faults")
    faults = _certificate_fault_injection(
        plan=plan,
        physical=physical,
        certificate=certificate,
        observed_result_digest=first.result_digest,
    )
    faults[1:1] = _policy_faults(
        plan=plan,
        physical=physical,
        certificate=certificate,
        observed_result_digest=first.result_digest,
        planner_digest=planner_digest,
        selected_candidate_id=required_candidate,
    )
    required_faults = set(protocol["required_faults"])
    if {item["fault_id"] for item in faults} != required_faults:
        raise MultisourceCaseStudyError("Frozen fault-injection matrix changed")

    report(9, 9, "writing machine evidence, human report, and reproduction commands")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = project_root / _RESULTS / run_id
    summary: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS_MULTISOURCE_CASE_STUDY_V2_COMPLETE_LOOP",
        "paper_performance_evidence": False,
        "scientific_scope": "complete_end_to_end_semantic_case_study",
        "run_id": run_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "protocol_path": _PROTOCOL.as_posix(),
        "protocol_sha256": sha256_file(project_root / _PROTOCOL),
        "artifact_verification": artifacts,
        "agent_validation": {
            "safe_status": safe_response.status.value,
            "safe_reason_codes": sorted(safe_reasons),
            "illegal_status": illegal_response.status.value,
            "illegal_reason_codes": sorted(illegal_reasons),
        },
        "validation": {
            "logical_plan_id": plan.logical_plan_id,
            "logical_plan_digest": plan.validation.canonical_digest,
            "status": safe_response.status.value,
        },
        "candidate_planning": {
            "generated_candidate_count": len(candidates),
            "generated_candidate_ids": list(candidate_ids),
            "feasible_candidate_ids": list(planning.feasible_candidate_ids),
            "rejected_candidate_ids": list(planning.rejected_candidate_ids),
            "nondominated_candidate_ids": list(planning.nondominated_candidate_ids),
            "selected_candidate_id": planning.selected_candidate_id,
            "reason_code": planning.reason_code,
            "planner_decision_digest": planner_digest,
            "performance_model_used": planning.performance_model_used,
            "governance_before_ranking": True,
        },
        "physical_plan": {
            "physical_plan_id": physical.physical_plan_id,
            "strategy_id": physical.strategy.strategy_id,
            "operator_count": len(physical.physical_operators),
            "compiled_sql_digest": "sha256:"
            + __import__("hashlib").sha256(compiled.sql.encode()).hexdigest(),
        },
        "execution": {
            "row_count": first.row_count,
            "result_digest": first.result_digest,
            "deterministic_repeat": True,
            "raw_sensitive_exposure_rows": raw_exposure_rows,
            "malformed_mask_count": malformed_masks,
        },
        "lineage": {
            "level": lineage.evidence.lineage_level.value,
            "source_count": lineage.source_count,
            "lineage_digest": lineage.lineage_digest,
            "record_lineage_v4_applied": False,
            "record_lineage_v4_boundary": protocol["lineage_scope"]["boundary"],
        },
        "certificate": {
            "status": certificate_check.status.value,
            "verified_obligations": [item.value for item in certificate_check.verified_obligations],
            "unverified_components": list(certificate_check.unverified_components),
            "diagnostics": [],
        },
        "fault_injection": faults,
        "claim_boundary": protocol["certificate_boundary"],
    }
    _atomic_json(run_dir / "agent_safe_request.json", safe_request)
    _atomic_json(run_dir / "agent_illegal_request.json", illegal_request)
    _atomic_json(run_dir / "validated_plan.json", plan.model_dump(mode="json"))
    _atomic_json(run_dir / "candidate_planning.json", asdict(planning))
    _atomic_json(run_dir / "approved_physical_plan.json", physical.model_dump(mode="json"))
    _atomic_json(run_dir / "certificate.json", certificate.model_dump(mode="json"))
    _atomic_json(run_dir / "summary.json", summary)
    (run_dir / "report.md").write_text(_report_markdown(summary), encoding="utf-8")
    _atomic_json(project_root / _RESULTS / "latest_run.json", {"run_id": run_id})
    return run_dir
