"""Audited end-to-end execution for the frozen four-source case study.

This module deliberately measures no latency.  Its job is to prove that one
real multi-source request can pass through TrustAero's validation, governance
rewrite, physical planning, DuckDB execution, source-lineage capture, and
independent certificate checking without exposing the controlled sensitive
field.  Performance claims belong to separate, already frozen experiments.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data.download import sha256_file
from trustaero.execution import (
    TableBindings,
    capture_source_lineage,
    compile_approved_physical_plan,
    execute_with_connection,
)
from trustaero.ir.enums import ObligationType, ReasonCode, ValidationStatus
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    ExecutionEvent,
    GovernedExecutionCertificate,
    PhysicalOperatorSpec,
    PolicySet,
    ValidatedLogicalPlan,
)
from trustaero.planner.physical import plan_physical_execution
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)
from trustaero.validator.service import validate


class MultisourceCaseStudyError(RuntimeError):
    """Raised when an end-to-end case-study invariant is violated."""


EventType = Literal[
    "PlanApproved",
    "OperatorStarted",
    "OperatorCompleted",
    "PolicyDecisionRecorded",
    "ResultMaterialized",
    "LineageRecorded",
    "CertificateEmitted",
]

_MANIFEST = Path("data/manifests/processed/multisource-case-v1.json")
_PROTOCOL = Path("experiments/frozen/multisource_case_study_query_protocol_v1_20260725.json")
_CATALOG = Path("examples/multisource/catalog.json")
_POLICY = Path("examples/multisource/policy.json")
_PLAN = Path("examples/multisource/plan.json")
_RESULTS = Path("results/multisource_case_study_v1")

_TABLE_BY_ARTIFACT = {
    "multisource_earthquakes_v1": ("usgs_earthquake", "trust_earthquakes"),
    "multisource_wells_v1": ("oil_gas_well", "trust_wells"),
    "multisource_airports_v1": ("airport", "trust_airports"),
    "multisource_cities_v1": ("city", "trust_cities"),
}


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise MultisourceCaseStudyError(f"Expected a JSON object: {path}")
    return loaded


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish complete JSON atomically so an interruption cannot corrupt evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sql_literal(value: Path | str) -> str:
    """Quote a trusted local path for a DuckDB DDL statement."""

    return "'" + str(value).replace("'", "''") + "'"


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git_state(project_root: Path) -> tuple[str, bool]:
    """Record which source tree produced the evidence without changing Git state."""

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MultisourceCaseStudyError("Cannot bind evidence to the Git source tree") from exc
    return commit, dirty


def verify_multisource_case_artifacts(
    project_root: Path,
    manifest_path: Path = _MANIFEST,
) -> dict[str, Any]:
    """Verify raw audits and every prepared Parquet file before query execution."""

    project_root = project_root.resolve()
    data_root = project_root / "data"
    manifest_path = project_root / manifest_path
    manifest = _load_json(manifest_path)

    verified_inputs: list[dict[str, Any]] = []
    for item in manifest.get("inputs", []):
        raw_path = data_root / str(item["relative_path"])
        audit_path = data_root / str(item["download_audit"])
        audit = _load_json(audit_path)
        digest = sha256_file(raw_path)
        if (
            raw_path.stat().st_size != int(item["byte_size"])
            or digest != item["sha256"]
            or audit.get("sha256") != digest
            or audit.get("byte_size") != raw_path.stat().st_size
        ):
            raise MultisourceCaseStudyError(
                f"Raw source no longer matches its frozen audit: {item['artifact_id']}"
            )
        verified_inputs.append(
            {
                "artifact_id": item["artifact_id"],
                "byte_size": raw_path.stat().st_size,
                "sha256": digest,
            }
        )

    verified_outputs: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    for item in manifest.get("outputs", []):
        artifact_id = str(item["artifact_id"])
        path = data_root / str(item["relative_path"])
        digest = sha256_file(path)
        if path.stat().st_size != int(item["byte_size"]) or digest != item["sha256"]:
            raise MultisourceCaseStudyError(
                f"Prepared table no longer matches its manifest: {artifact_id}"
            )
        seen_artifacts.add(artifact_id)
        verified_outputs.append(
            {
                "artifact_id": artifact_id,
                "relative_path": item["relative_path"],
                "row_count": int(item["row_count"]),
                "byte_size": path.stat().st_size,
                "sha256": digest,
            }
        )
    if seen_artifacts != set(_TABLE_BY_ARTIFACT):
        raise MultisourceCaseStudyError(
            "Prepared manifest does not contain exactly the four frozen case-study tables"
        )

    return {
        "manifest_sha256": sha256_file(manifest_path),
        "inputs": verified_inputs,
        "outputs": verified_outputs,
    }


def _create_trusted_views(
    connection: Any,
    project_root: Path,
    artifacts: dict[str, Any],
) -> TableBindings:
    """Bind only hash-verified Parquet files to catalog dataset identifiers."""

    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    dataset_tables: dict[str, str] = {}
    for item in artifacts["outputs"]:
        dataset_id, view_name = _TABLE_BY_ARTIFACT[str(item["artifact_id"])]
        path = project_root / "data" / str(item["relative_path"])
        connection.execute(
            f"CREATE OR REPLACE TEMP VIEW {view_name} AS "
            f"SELECT * FROM read_parquet({_sql_literal(path)})"
        )
        dataset_tables[dataset_id] = view_name
    return TableBindings(dataset_tables=dataset_tables)


def _event(
    sequence: int,
    event_type: EventType,
    payload_digest: str,
    operator_id: str | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        sequence=sequence,
        event_type=event_type,
        operator_id=operator_id,
        payload_digest=payload_digest,
    )


def _topological_operators(
    physical: ApprovedPhysicalPlan,
) -> tuple[PhysicalOperatorSpec, ...]:
    """Create a deterministic event order that respects every physical input edge."""

    remaining = {
        operator.physical_operator_id: operator for operator in physical.physical_operators
    }
    completed: set[str] = set()
    ordered: list[PhysicalOperatorSpec] = []
    while remaining:
        ready = sorted(
            (
                operator
                for operator in remaining.values()
                if set(operator.inputs).issubset(completed)
            ),
            key=lambda operator: operator.physical_operator_id,
        )
        if not ready:
            raise MultisourceCaseStudyError("Approved physical plan is cyclic or unbound")
        for operator in ready:
            ordered.append(operator)
            completed.add(operator.physical_operator_id)
            del remaining[operator.physical_operator_id]
    return tuple(ordered)


def _certificate_events(
    physical: ApprovedPhysicalPlan,
    *,
    policy_snapshot: str,
    result_digest: str,
    lineage_digest: str,
) -> tuple[ExecutionEvent, ...]:
    events: list[ExecutionEvent] = [_event(0, "PlanApproved", physical.physical_plan_id)]
    events.append(_event(1, "PolicyDecisionRecorded", policy_snapshot))
    sequence = 2
    for operator in _topological_operators(physical):
        events.append(
            _event(
                sequence,
                "OperatorStarted",
                _text_digest(f"start:{operator.physical_operator_id}"),
                operator.physical_operator_id,
            )
        )
        sequence += 1
        events.append(
            _event(
                sequence,
                "OperatorCompleted",
                _text_digest(f"complete:{operator.physical_operator_id}"),
                operator.physical_operator_id,
            )
        )
        sequence += 1
    events.append(_event(sequence, "ResultMaterialized", result_digest))
    sequence += 1
    events.append(_event(sequence, "LineageRecorded", lineage_digest))
    sequence += 1
    events.append(_event(sequence, "CertificateEmitted", _text_digest("certificate")))
    return tuple(events)


def _diagnostic_codes(check: Any) -> tuple[str, ...]:
    return tuple(sorted(item.code.value for item in check.diagnostics))


def _require_rejected_fault(
    *,
    fault_id: str,
    check: Any,
    expected_code: ReasonCode,
) -> dict[str, Any]:
    codes = _diagnostic_codes(check)
    if check.status != CertificateVerificationStatus.REJECT or expected_code.value not in codes:
        raise MultisourceCaseStudyError(
            f"Fault {fault_id} was not rejected as {expected_code.value}: {codes}"
        )
    return {
        "fault_id": fault_id,
        "status": check.status.value,
        "expected_reason_code": expected_code.value,
        "actual_reason_codes": codes,
    }


def _dependency_tampered_events(
    physical: ApprovedPhysicalPlan,
    events: tuple[ExecutionEvent, ...],
) -> tuple[ExecutionEvent, ...]:
    """Move one child start before an input completion while keeping sequences valid."""

    dependent = next(operator for operator in _topological_operators(physical) if operator.inputs)
    dependency_id = dependent.inputs[0]
    child_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "OperatorStarted"
        and event.operator_id == dependent.physical_operator_id
    )
    dependency_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "OperatorCompleted" and event.operator_id == dependency_id
    )
    changed = list(events)
    child = changed[child_index]
    dependency = changed[dependency_index]
    changed[child_index] = child.model_copy(update={"sequence": dependency.sequence})
    changed[dependency_index] = dependency.model_copy(update={"sequence": child.sequence})
    return tuple(sorted(changed, key=lambda event: event.sequence))


def _certificate_fault_injection(
    *,
    plan: ValidatedLogicalPlan,
    physical: ApprovedPhysicalPlan,
    certificate: GovernedExecutionCertificate,
    observed_result_digest: str,
) -> list[dict[str, Any]]:
    """Prove that a certificate cannot validate its own forged claims."""

    wrong_digest = "sha256:" + "0" * 64
    result_check = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest=wrong_digest,
    )

    snapshots = dict(certificate.data_snapshots)
    first_dataset = sorted(snapshots)[0]
    snapshots[first_dataset] = wrong_digest
    snapshot_check = verify_execution_certificate(
        plan,
        physical,
        certificate.model_copy(update={"data_snapshots": snapshots}),
        observed_result_digest=observed_result_digest,
    )

    if certificate.lineage_evidence is None:
        raise MultisourceCaseStudyError("Cannot inject a lineage fault without evidence")
    lineage_evidence = certificate.lineage_evidence.model_copy(update={"covered_operators": ()})
    lineage_check = verify_execution_certificate(
        plan,
        physical,
        certificate.model_copy(update={"lineage_evidence": lineage_evidence}),
        observed_result_digest=observed_result_digest,
    )

    dependency_check = verify_execution_certificate(
        plan,
        physical,
        certificate.model_copy(
            update={"events": _dependency_tampered_events(physical, certificate.events)}
        ),
        observed_result_digest=observed_result_digest,
    )
    return [
        _require_rejected_fault(
            fault_id="observed_result_digest_mismatch",
            check=result_check,
            expected_code=ReasonCode.CERTIFICATE_RESULT_DIGEST_MISMATCH,
        ),
        _require_rejected_fault(
            fault_id="data_snapshot_tamper",
            check=snapshot_check,
            expected_code=ReasonCode.CERTIFICATE_SNAPSHOT_MISMATCH,
        ),
        _require_rejected_fault(
            fault_id="lineage_coverage_removed",
            check=lineage_check,
            expected_code=ReasonCode.LINEAGE_TARGET_NOT_COVERED,
        ),
        _require_rejected_fault(
            fault_id="physical_dependency_order_tamper",
            check=dependency_check,
            expected_code=ReasonCode.CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION,
        ),
    ]


def run_multisource_case_study(
    project_root: Path,
    *,
    progress: bool = False,
    require_clean: bool = False,
) -> Path:
    """Run and persist the frozen, non-performance four-source smoke evidence."""

    project_root = project_root.resolve()
    commit, dirty = _git_state(project_root)
    if require_clean and dirty:
        raise MultisourceCaseStudyError(
            "Publication evidence requires a clean Git tree; commit the reviewed "
            "implementation before rerunning with --require-clean"
        )

    def report(step: int, total: int, message: str) -> None:
        if progress:
            print(f"[Multisource {step:02d}/{total:02d}] {message}", flush=True)

    report(1, 7, "verifying raw and prepared artifact hashes")
    artifacts = verify_multisource_case_artifacts(project_root)
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(project_root / _CATALOG)))
    policy = PolicySet.model_validate(_load_json(project_root / _POLICY))
    raw_plan = _load_json(project_root / _PLAN)

    report(2, 7, "validating policy and deterministic governance rewrites")
    response = validate(raw_plan, policy, catalog)
    reason_codes = tuple(sorted(item.code.value for item in response.diagnostics))
    required_reasons = {
        ReasonCode.MASK_REQUIRED.value,
        ReasonCode.LINEAGE_REQUIRED.value,
    }
    if (
        response.status != ValidationStatus.REWRITE
        or response.validated_plan is None
        or set(reason_codes) != required_reasons
    ):
        raise MultisourceCaseStudyError(
            f"Frozen plan expected REWRITE/{sorted(required_reasons)}, got "
            f"{response.status.value}/{reason_codes}"
        )
    plan = response.validated_plan
    operator_types = [operator.operator_type for operator in plan.operators]
    if operator_types.count("Mask") != 1 or operator_types.count("LineageCapture") != 1:
        raise MultisourceCaseStudyError(
            "Governance rewrite must insert one Mask and one lineage node"
        )

    report(3, 7, "planning and compiling the approved DuckDB physical plan")
    physical = plan_physical_execution(plan, backend="duckdb")
    if physical.unimplemented_backend_features:
        raise MultisourceCaseStudyError(
            f"Physical plan contains unsupported features: "
            f"{physical.unimplemented_backend_features}"
        )

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MultisourceCaseStudyError("DuckDB is required; run inside TrustAero_env") from exc

    connection = duckdb.connect()
    try:
        bindings = _create_trusted_views(connection, project_root, artifacts)
        compiled = compile_approved_physical_plan(
            plan,
            physical,
            catalog,
            bindings,
        )

        report(4, 7, "executing twice and checking deterministic governed output")
        first = execute_with_connection(compiled, connection)
        second = execute_with_connection(compiled, connection)
        if (
            first.row_count <= 0
            or first.row_count != second.row_count
            or first.result_digest != second.result_digest
        ):
            raise MultisourceCaseStudyError(
                "Repeated executions must return the same non-empty result"
            )

        try:
            sensitive_index = first.columns.index("api_well_number")
        except ValueError as exc:
            raise MultisourceCaseStudyError(
                "Governed output lost the controlled sensitive field"
            ) from exc
        raw_values = {
            str(row[0])
            for row in connection.execute(
                "SELECT api_well_number FROM trust_wells WHERE api_well_number IS NOT NULL"
            ).fetchall()
        }
        masked_values = [
            str(row[sensitive_index]) for row in first.rows if row[sensitive_index] is not None
        ]
        malformed_mask_count = sum(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in masked_values
        )
        raw_sensitive_exposure_rows = sum(value in raw_values for value in masked_values)
        if not masked_values or malformed_mask_count or raw_sensitive_exposure_rows:
            raise MultisourceCaseStudyError(
                "Governed output violates the frozen SHA-256 presentation contract"
            )
    finally:
        connection.close()

    report(5, 7, "capturing source lineage and verifying the execution certificate")
    execution_id = "exec-multisource-case-v1"
    lineage = capture_source_lineage(
        plan,
        execution_id=execution_id,
        result_id=first.result_digest,
    )
    if lineage.evidence is None or lineage.lineage_digest is None or lineage.source_count != 4:
        raise MultisourceCaseStudyError("Source lineage must cover all four contributing scans")
    certificate = GovernedExecutionCertificate(
        certificate_id="cert-multisource-case-v1",
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
    )
    certificate_check = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest=first.result_digest,
    )
    if (
        certificate_check.status != CertificateVerificationStatus.PARTIAL
        or certificate_check.diagnostics
        or ObligationType.LINEAGE_CAPTURE not in certificate_check.verified_obligations
    ):
        raise MultisourceCaseStudyError(
            f"Independent certificate check failed: {_diagnostic_codes(certificate_check)}"
        )

    report(6, 7, "injecting result, snapshot, lineage, and dependency faults")
    faults = _certificate_fault_injection(
        plan=plan,
        physical=physical,
        certificate=certificate,
        observed_result_digest=first.result_digest,
    )

    report(7, 7, "writing auditable non-performance evidence")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = project_root / _RESULTS / run_id
    summary: dict[str, Any] = {
        "status": "PASS_MULTISOURCE_CASE_STUDY_END_TO_END",
        "paper_performance_evidence": False,
        "scientific_scope": "end_to_end_semantic_case_study",
        "run_id": run_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "protocol_path": _PROTOCOL.as_posix(),
        "protocol_sha256": sha256_file(project_root / _PROTOCOL),
        "artifact_verification": artifacts,
        "validation": {
            "status": response.status.value,
            "reason_codes": reason_codes,
            "logical_plan_id": plan.logical_plan_id,
            "logical_plan_digest": plan.validation.canonical_digest,
            "operator_types": operator_types,
        },
        "physical_plan": {
            "physical_plan_id": physical.physical_plan_id,
            "operator_count": len(physical.physical_operators),
            "unimplemented_backend_features": list(physical.unimplemented_backend_features),
            "compiled_sql_digest": _text_digest(compiled.sql),
        },
        "execution": {
            "repeat_count": 2,
            "row_count": first.row_count,
            "result_digest": first.result_digest,
            "deterministic_repeat": True,
            "masked_non_null_count": len(masked_values),
            "malformed_mask_count": malformed_mask_count,
            "raw_sensitive_exposure_rows": raw_sensitive_exposure_rows,
        },
        "lineage": {
            "level": lineage.evidence.lineage_level.value,
            "source_count": lineage.source_count,
            "lineage_digest": lineage.lineage_digest,
        },
        "certificate": {
            "status": certificate_check.status.value,
            "verified_obligations": [item.value for item in certificate_check.verified_obligations],
            "unverified_components": list(certificate_check.unverified_components),
            "diagnostics": [],
        },
        "fault_injection": faults,
        "claim_boundary": (
            "This run proves structural and semantic integration over four real public "
            "sources. It does not measure optimizer speed or cryptographically attest "
            "the internal DuckDB operator outputs."
        ),
    }
    _atomic_json(run_dir / "validated_plan.json", plan.model_dump(mode="json"))
    _atomic_json(run_dir / "approved_physical_plan.json", physical.model_dump(mode="json"))
    _atomic_json(run_dir / "certificate.json", certificate.model_dump(mode="json"))
    _atomic_json(run_dir / "summary.json", summary)
    _atomic_json(project_root / _RESULTS / "latest_run.json", {"run_id": run_id})
    return run_dir
