"""Approved multi-candidate semantic smoke for BTS and NYC real data.

The smoke does not time candidates.  It proves, in order, that each SQL route
comes from an ApprovedPhysicalPlan, passes hard governance feasibility, returns
the same unordered relation, produces a distinct observed DuckDB structure, and
can be bound to independently checked lineage/certificate evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_real_data_slice_artifacts
from trustaero.execution import (
    QueryExecutionResult,
    capture_source_lineage,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _certificate_events,
    _create_trusted_views,
    _load_json,
)
from trustaero.experiments.real_data_pilot import _semantic_digest, _stage_statistics
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    GovernedExecutionCertificate,
    PolicySet,
    ValidatedLogicalPlan,
)
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.certificate import (
    CertificateVerificationStatus,
    verify_execution_certificate,
)
from trustaero.validator.service import validate

_TARGETS = {
    "bts": ("bts-filter", "gov-002-mask"),
    "nyc_tlc": ("nyc-filter", "nyc-zone-join"),
}


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raw_plan(examples: Path, workload: str) -> dict[str, Any]:
    filename = "bts_governed_read.json" if workload == "bts" else "nyc_governed_aggregate.json"
    return _load_json(examples / "plans" / filename)


def _candidate_exposure(
    *,
    workload: str,
    strategy_id: str,
    materialize_after: tuple[str, ...],
    governed_rows: int,
) -> CandidateExposure:
    """Derive exposure only from the reviewed strategy allow-list and statistics."""

    if strategy_id == "fused" and not materialize_after:
        return CandidateExposure(strategy_id, 0, 0, 0)
    if len(materialize_after) != 1 or materialize_after[0] not in _TARGETS[workload]:
        raise GovernedRealDataSmokeError(f"Unreviewed candidate exposure: {strategy_id}")
    target = materialize_after[0]
    if workload == "bts" and target == "bts-filter":
        return CandidateExposure(strategy_id, 0, governed_rows, 0)
    if workload == "bts" and target == "gov-002-mask":
        return CandidateExposure(strategy_id, 0, 0, governed_rows)
    # The current NYC catalog does not mark either location key as sensitive.
    # Therefore these boundaries have no raw-sensitive exposure under this
    # policy fragment; this is not a claim that identifiers are never sensitive.
    return CandidateExposure(strategy_id, 0, 0, 0)


def verify_candidate_execution_certificate(
    logical: ValidatedLogicalPlan,
    candidate: ApprovedPhysicalPlan,
    execution: QueryExecutionResult,
    *,
    execution_id: str,
) -> str:
    """Bind one observed candidate result to lineage and a checked certificate."""

    lineage = capture_source_lineage(
        logical,
        execution_id=execution_id,
        result_id=execution.result_digest,
    )
    if lineage.evidence is None or lineage.lineage_digest is None:
        raise GovernedRealDataSmokeError("Candidate lineage evidence is missing")
    events = _certificate_events(
        candidate,
        policy_snapshot=logical.bindings.policy_snapshot,
        result_digest=execution.result_digest,
        lineage_digest=lineage.lineage_digest,
    )
    certificate = GovernedExecutionCertificate(
        certificate_id=f"cert-{execution_id}",
        task_digest=logical.validation.canonical_digest,
        logical_plan_id=logical.logical_plan_id,
        physical_plan_id=candidate.physical_plan_id,
        policy_snapshot=logical.bindings.policy_snapshot,
        data_snapshots=logical.bindings.data_snapshots,
        events=events,
        result_digest=execution.result_digest,
        lineage_evidence=lineage.evidence,
        lineage_digest=lineage.lineage_digest,
    )
    check = verify_execution_certificate(
        logical,
        candidate,
        certificate,
        observed_result_digest=execution.result_digest,
    )
    if check.status != CertificateVerificationStatus.PARTIAL:
        raise GovernedRealDataSmokeError("Candidate certificate did not verify")
    return check.status.value


def run_real_data_candidate_smoke(
    project_root: Path,
    *,
    sample_rows: int = 100_000,
) -> dict[str, Any]:
    """Validate, execute, and certificate three candidates per workload."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for candidate smoke") from exc

    root = project_root.resolve()
    artifact_bindings = verify_real_data_slice_artifacts(root / "data", sample_rows)
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    profiles = (
        GovernanceFeasibilityPolicy(
            policy_id="output-mask-only",
            max_raw_join_rows=None,
            max_raw_materialized_rows=None,
        ),
        GovernanceFeasibilityPolicy(
            policy_id="no-raw-sensitive-materialization",
            max_raw_join_rows=None,
            max_raw_materialized_rows=0,
        ),
    )
    connection = duckdb.connect()
    workload_results: list[dict[str, Any]] = []
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET threads = 4")
        bindings = _create_trusted_views(
            connection,
            root / "data",
            sample_rows=sample_rows,
        )
        for workload in ("bts", "nyc_tlc"):
            response = validate(_raw_plan(examples, workload), policy, catalog)
            if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
                raise GovernedRealDataSmokeError(f"{workload} candidate plan was not approved")
            logical: ValidatedLogicalPlan | None = response.validated_plan
            if logical is None:
                raise GovernedRealDataSmokeError(f"{workload} has no validated plan")
            stage_statistics = _stage_statistics(connection, workload)
            governed_rows = int(stage_statistics["governed_rows"])
            candidates = generate_duckdb_candidates(
                logical,
                materialization_targets=_TARGETS[workload],
            )
            exposures = tuple(
                _candidate_exposure(
                    workload=workload,
                    strategy_id=candidate.strategy.strategy_id,
                    materialize_after=candidate.strategy.materialize_after,
                    governed_rows=governed_rows,
                )
                for candidate in candidates
            )
            feasibility = {
                profile.policy_id: filter_feasible_candidates(exposures, profile)
                for profile in profiles
            }

            expected_semantic_digest: str | None = None
            candidate_results: list[dict[str, Any]] = []
            fingerprints: set[str] = set()
            for candidate, exposure in zip(candidates, exposures, strict=True):
                # Performance never influences this decision: output-mask-only
                # is the active permissive profile for this semantic smoke.
                active = feasibility["output-mask-only"]
                if candidate.strategy.strategy_id not in active.feasible_candidate_ids:
                    raise GovernedRealDataSmokeError("Active policy rejected candidate")
                compiled = compile_approved_physical_plan(
                    logical,
                    candidate,
                    catalog,
                    bindings,
                )
                execution = execute_with_connection(compiled, connection)
                semantic_digest = _semantic_digest(execution.columns, execution.rows)
                if expected_semantic_digest is None:
                    expected_semantic_digest = semantic_digest
                elif semantic_digest != expected_semantic_digest:
                    raise GovernedRealDataSmokeError(
                        f"{workload} approved candidates returned different relations"
                    )
                observation = observe_duckdb_plan(
                    connection,
                    compiled.sql,
                    compiled.parameters,
                    analyze=False,
                )
                if observation.fingerprint in fingerprints:
                    raise GovernedRealDataSmokeError(
                        f"{workload} candidate collapsed to a duplicate DuckDB plan"
                    )
                fingerprints.add(observation.fingerprint)

                execution_id = f"exec-candidate-{workload}-{candidate.strategy.strategy_id}"
                certificate_status = verify_candidate_execution_certificate(
                    logical,
                    candidate,
                    execution,
                    execution_id=execution_id,
                )
                candidate_results.append(
                    {
                        "strategy_id": candidate.strategy.strategy_id,
                        "physical_plan_id": candidate.physical_plan_id,
                        "compiled_physical_plan_id": compiled.physical_plan_id,
                        "execution_mode": candidate.strategy.execution_mode,
                        "materialize_after": list(candidate.strategy.materialize_after),
                        "sql_digest": _digest_text(compiled.sql),
                        "semantic_result_digest": semantic_digest,
                        "output_row_count": execution.row_count,
                        "duckdb_plan_fingerprint": observation.fingerprint,
                        "duckdb_operator_names": list(observation.operator_names),
                        "certificate_status": certificate_status,
                        "exposure": asdict(exposure),
                    }
                )

            strict_result = feasibility["no-raw-sensitive-materialization"]
            workload_results.append(
                {
                    "workload": workload,
                    "sample_rows": sample_rows,
                    "status": "PASS",
                    "candidate_count": len(candidates),
                    "distinct_duckdb_plan_count": len(fingerprints),
                    "stage_statistics": stage_statistics,
                    "candidates": candidate_results,
                    "feasibility_profiles": {
                        name: {
                            "status": result.status,
                            "feasible_candidate_ids": list(result.feasible_candidate_ids),
                            "rejected_candidate_ids": list(result.rejected_candidate_ids),
                            "decisions": [asdict(item) for item in result.decisions],
                        }
                        for name, result in feasibility.items()
                    },
                    "strict_profile_rejected_raw_boundary": (
                        "materialize-after-bts-filter" in strict_result.rejected_candidate_ids
                        if workload == "bts"
                        else not strict_result.rejected_candidate_ids
                    ),
                }
            )
    finally:
        connection.close()

    payload = {
        "schema_version": 1,
        "status": "PASS",
        "purpose": "approved real-data candidate semantic smoke; no performance timing",
        "paper_performance_evidence": False,
        "verified_execution_artifacts": [asdict(item) for item in artifact_bindings],
        "workloads": workload_results,
    }
    _atomic_json(
        root / "data/manifests/processed/real-data-candidate-smoke.json",
        payload,
    )
    return payload
