"""End-to-end TrustAero governance smoke over approved real-data slices.

The runner uses only TrustAero's public validation, compilation, physical
planning, lineage, execution, and certificate-verification boundaries. It
deliberately records no latency: the 100K stage proves semantic integration and
fail-closed behaviour before any paper performance experiment is permitted.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_real_data_slice_artifacts
from trustaero.execution import (
    TableBindings,
    capture_source_lineage,
    compile_validated_plan,
    execute_with_connection,
)
from trustaero.ir.enums import ReasonCode, ValidationStatus
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


class GovernedRealDataSmokeError(RuntimeError):
    """Raised when a governed real-data smoke invariant is not met."""


ExecutionEventType = Literal[
    "PlanApproved",
    "OperatorStarted",
    "OperatorCompleted",
    "PolicyDecisionRecorded",
    "ResultMaterialized",
    "LineageRecorded",
    "CertificateEmitted",
]


@dataclass(frozen=True, slots=True)
class GovernedCaseResult:
    """Small auditable summary without raw result values or performance timing."""

    case_id: str
    validation_status: str
    reason_codes: tuple[str, ...]
    row_count: int
    result_digest: str
    compiled_sql_digest: str
    certificate_status: str
    verified_obligations: tuple[str, ...]
    policy_snapshot: str
    data_snapshots: dict[str, str]
    lineage_source_count: int
    masked_non_null_count: int
    raw_sensitive_exposure_rows: int


@dataclass(frozen=True, slots=True)
class NegativeCaseResult:
    """Expected fail-closed outcome for one mutated real-data plan."""

    case_id: str
    expected_status: str
    actual_status: str
    expected_reason_code: str
    actual_reason_codes: tuple[str, ...]


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise GovernedRealDataSmokeError(f"Expected a JSON object: {path}")
    return loaded


def _sql_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event(
    sequence: int,
    event_type: ExecutionEventType,
    payload_digest: str,
    operator_id: str | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        sequence=sequence,
        event_type=event_type,
        operator_id=operator_id,
        payload_digest=payload_digest,
    )


def _topological_physical_operators(
    plan: ApprovedPhysicalPlan,
) -> tuple[PhysicalOperatorSpec, ...]:
    """Derive a stable dependency-respecting event order from the physical DAG."""

    remaining = {item.physical_operator_id: item for item in plan.physical_operators}
    ordered: list[PhysicalOperatorSpec] = []
    completed: set[str] = set()
    while remaining:
        ready = sorted(
            (item for item in remaining.values() if set(item.inputs).issubset(completed)),
            key=lambda item: item.physical_operator_id,
        )
        if not ready:
            raise GovernedRealDataSmokeError("Approved physical plan is cyclic or unbound")
        for item in ready:
            ordered.append(item)
            completed.add(item.physical_operator_id)
            del remaining[item.physical_operator_id]
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
    for operator in _topological_physical_operators(physical):
        events.append(
            _event(
                sequence,
                "OperatorStarted",
                _digest_text(f"start:{operator.physical_operator_id}"),
                operator.physical_operator_id,
            )
        )
        sequence += 1
        events.append(
            _event(
                sequence,
                "OperatorCompleted",
                _digest_text(f"complete:{operator.physical_operator_id}"),
                operator.physical_operator_id,
            )
        )
        sequence += 1
    events.append(_event(sequence, "ResultMaterialized", result_digest))
    sequence += 1
    events.append(_event(sequence, "LineageRecorded", lineage_digest))
    sequence += 1
    events.append(_event(sequence, "CertificateEmitted", _digest_text("certificate")))
    return tuple(events)


def _create_trusted_views(
    connection: Any,
    data_root: Path,
    *,
    sample_rows: int = 100_000,
) -> TableBindings:
    """Expose only catalog-declared fields through trusted, typed DuckDB views."""

    if sample_rows < 1:
        raise ValueError("sample_rows must be positive")
    bts = data_root / f"processed/bts/on_time/2024-01/bts_flights_{sample_rows}.parquet"
    nyc = data_root / f"processed/nyc_tlc/yellow/2024-01/yellow_taxi_{sample_rows}.parquet"
    zones = data_root / "processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet"
    for path in (bts, nyc, zones):
        if not path.is_file():
            raise GovernedRealDataSmokeError(f"Prepared 100K artifact is missing: {path}")

    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_flights AS SELECT "
        "CAST(Tail_Number AS VARCHAR) AS Tail_Number, "
        "CAST(FlightDate AS TIMESTAMPTZ) AS FlightDate, "
        "CAST(Origin AS VARCHAR) AS Origin, CAST(Dest AS VARCHAR) AS Dest, "
        "CAST(Distance AS DOUBLE) AS Distance, CAST(Cancelled AS BOOLEAN) AS Cancelled "
        f"FROM read_parquet({_sql_literal(bts)})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_nyc_trips AS SELECT "
        "CAST(PULocationID AS BIGINT) AS PULocationID, "
        "CAST(tpep_pickup_datetime AS TIMESTAMPTZ) AS tpep_pickup_datetime, "
        "CAST(trip_distance AS DOUBLE) AS trip_distance, "
        # Currency is normalized to a fixed two-decimal representation.  A
        # parallel SUM over binary floats may vary in its last bits solely due
        # to thread merge order, which would make certificate digests unstable.
        "CAST(total_amount AS DECIMAL(18, 2)) AS total_amount "
        f"FROM read_parquet({_sql_literal(nyc)})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_nyc_zones AS SELECT "
        "CAST(LocationID AS BIGINT) AS LocationID, CAST(Borough AS VARCHAR) AS Borough, "
        "CAST(service_zone AS VARCHAR) AS service_zone "
        f"FROM read_parquet({_sql_literal(zones)})"
    )
    return TableBindings(
        dataset_tables={
            "bts_on_time_2024_01": "trust_bts_flights",
            "nyc_tlc_yellow_2024_01": "trust_nyc_trips",
            "nyc_tlc_taxi_zones": "trust_nyc_zones",
        }
    )


def _create_full_month_views(
    connection: Any,
    data_root: Path,
    *,
    workload: str,
) -> TableBindings:
    """Bind only the files required by one immutable full-month workload."""

    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    if workload == "bts":
        bts = data_root / "processed/bts/on_time/2024-01/bts_flights_full.parquet"
        connection.execute(
            "CREATE OR REPLACE TEMP VIEW trust_bts_flights AS SELECT "
            "CAST(Tail_Number AS VARCHAR) AS Tail_Number, "
            "CAST(FlightDate AS TIMESTAMPTZ) AS FlightDate, "
            "CAST(Origin AS VARCHAR) AS Origin, CAST(Dest AS VARCHAR) AS Dest, "
            "CAST(Distance AS DOUBLE) AS Distance, CAST(Cancelled AS BOOLEAN) AS Cancelled "
            f"FROM read_parquet({_sql_literal(bts)})"
        )
        return TableBindings(dataset_tables={"bts_on_time_2024_01": "trust_bts_flights"})
    if workload != "nyc_tlc":
        raise ValueError(f"unsupported full-month workload: {workload}")
    nyc = data_root / "raw/nyc_tlc/yellow/2024/yellow_tripdata_2024-01.parquet"
    zones = data_root / "processed/nyc_tlc/yellow/2024-01/taxi_zones.parquet"
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_nyc_trips AS SELECT "
        "CAST(PULocationID AS BIGINT) AS PULocationID, "
        "CAST(tpep_pickup_datetime AS TIMESTAMPTZ) AS tpep_pickup_datetime, "
        "CAST(trip_distance AS DOUBLE) AS trip_distance, "
        "CAST(total_amount AS DECIMAL(18, 2)) AS total_amount "
        f"FROM read_parquet({_sql_literal(nyc)})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_nyc_zones AS SELECT "
        "CAST(LocationID AS BIGINT) AS LocationID, CAST(Borough AS VARCHAR) AS Borough, "
        "CAST(service_zone AS VARCHAR) AS service_zone "
        f"FROM read_parquet({_sql_literal(zones)})"
    )
    return TableBindings(
        dataset_tables={
            "nyc_tlc_yellow_2024_01": "trust_nyc_trips",
            "nyc_tlc_taxi_zones": "trust_nyc_zones",
        }
    )


def _execute_governed_case(
    *,
    case_id: str,
    raw_plan: dict[str, Any],
    policy: PolicySet,
    catalog: InMemoryCatalog,
    bindings: TableBindings,
    connection: Any,
    expect_masked_tail: bool,
) -> GovernedCaseResult:
    response = validate(raw_plan, policy, catalog)
    if response.status not in {ValidationStatus.ACCEPT, ValidationStatus.REWRITE}:
        codes = [item.code.value for item in response.diagnostics]
        raise GovernedRealDataSmokeError(f"{case_id} failed validation: {codes}")
    plan: ValidatedLogicalPlan | None = response.validated_plan
    if plan is None:
        raise GovernedRealDataSmokeError(f"{case_id} returned no validated logical plan")

    compiled = compile_validated_plan(plan, catalog, bindings)
    physical = plan_physical_execution(plan, backend="duckdb")
    if physical.unimplemented_backend_features:
        raise GovernedRealDataSmokeError(
            f"{case_id} has unimplemented backend features: "
            f"{physical.unimplemented_backend_features}"
        )
    execution = execute_with_connection(compiled, connection)
    if execution.row_count <= 0:
        raise GovernedRealDataSmokeError(f"{case_id} produced an empty governed result")

    execution_id = f"exec-{case_id}"
    lineage = capture_source_lineage(
        plan,
        execution_id=execution_id,
        result_id=execution.result_digest,
    )
    if lineage.evidence is None or lineage.lineage_digest is None:
        raise GovernedRealDataSmokeError(f"{case_id} did not produce required lineage evidence")
    events = _certificate_events(
        physical,
        policy_snapshot=plan.bindings.policy_snapshot,
        result_digest=execution.result_digest,
        lineage_digest=lineage.lineage_digest,
    )
    certificate = GovernedExecutionCertificate(
        certificate_id=f"cert-{case_id}",
        task_digest=plan.validation.canonical_digest,
        logical_plan_id=plan.logical_plan_id,
        physical_plan_id=physical.physical_plan_id,
        policy_snapshot=plan.bindings.policy_snapshot,
        data_snapshots=plan.bindings.data_snapshots,
        events=events,
        result_digest=execution.result_digest,
        lineage_evidence=lineage.evidence,
        lineage_digest=lineage.lineage_digest,
    )
    certificate_check = verify_execution_certificate(
        plan,
        physical,
        certificate,
        observed_result_digest=execution.result_digest,
    )
    if certificate_check.status != CertificateVerificationStatus.PARTIAL:
        codes = [item.code.value for item in certificate_check.diagnostics]
        raise GovernedRealDataSmokeError(f"{case_id} certificate failed: {codes}")

    masked_non_null_count = 0
    raw_sensitive_exposure_rows = 0
    if expect_masked_tail:
        try:
            tail_index = execution.columns.index("Tail_Number")
        except ValueError as exc:
            raise GovernedRealDataSmokeError("BTS governed output lost Tail_Number") from exc
        for row in execution.rows:
            value = row[tail_index]
            if value is None:
                continue
            masked_non_null_count += 1
            text = str(value)
            if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
                raw_sensitive_exposure_rows += 1
        if masked_non_null_count == 0 or raw_sensitive_exposure_rows:
            raise GovernedRealDataSmokeError(
                "BTS output did not enforce the frozen SHA-256 presentation contract"
            )

    return GovernedCaseResult(
        case_id=case_id,
        validation_status=response.status.value,
        reason_codes=tuple(item.code.value for item in response.diagnostics),
        row_count=execution.row_count,
        result_digest=execution.result_digest,
        compiled_sql_digest=_digest_text(compiled.sql),
        certificate_status=certificate_check.status.value,
        verified_obligations=tuple(item.value for item in certificate_check.verified_obligations),
        policy_snapshot=plan.bindings.policy_snapshot,
        data_snapshots=plan.bindings.data_snapshots,
        lineage_source_count=lineage.source_count,
        masked_non_null_count=masked_non_null_count,
        raw_sensitive_exposure_rows=raw_sensitive_exposure_rows,
    )


def _check_negative_case(
    *,
    case_id: str,
    raw_plan: dict[str, Any],
    policy: PolicySet,
    catalog: InMemoryCatalog,
    expected_status: ValidationStatus,
    expected_reason: ReasonCode,
) -> NegativeCaseResult:
    response = validate(raw_plan, policy, catalog)
    actual_codes = tuple(item.code.value for item in response.diagnostics)
    if response.status != expected_status or expected_reason.value not in actual_codes:
        raise GovernedRealDataSmokeError(
            f"Negative case {case_id} expected {expected_status.value}/{expected_reason.value}, "
            f"received {response.status.value}/{actual_codes}"
        )
    return NegativeCaseResult(
        case_id=case_id,
        expected_status=expected_status.value,
        actual_status=response.status.value,
        expected_reason_code=expected_reason.value,
        actual_reason_codes=actual_codes,
    )


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def run_governed_real_data_smoke(project_root: Path) -> dict[str, Any]:
    """Run two governed executions and four fail-closed cases on 100K slices."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for this smoke") from exc

    root = project_root.resolve()
    artifact_bindings = verify_real_data_slice_artifacts(root / "data", 100_000)
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load_json(examples / "catalog.json")))
    policy = PolicySet.model_validate(_load_json(examples / "policy.json"))
    bts_plan = _load_json(examples / "plans/bts_governed_read.json")
    nyc_plan = _load_json(examples / "plans/nyc_governed_aggregate.json")
    illegal_mask = _load_json(examples / "plans/bts_illegal_mask_reuse.json")

    connection = duckdb.connect()
    try:
        connection.execute("SET memory_limit = '4GB'")
        # Keep possible DuckDB spill files on the project drive.  Creating the
        # directory explicitly also makes clean-room and CI runs deterministic.
        (root / "data/tmp/duckdb").mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(root / 'data/tmp/duckdb')}")
        bindings = _create_trusted_views(connection, root / "data")
        governed = [
            _execute_governed_case(
                case_id="REAL-BTS-100K",
                raw_plan=bts_plan,
                policy=policy,
                catalog=catalog,
                bindings=bindings,
                connection=connection,
                expect_masked_tail=True,
            ),
            _execute_governed_case(
                case_id="REAL-NYC-100K",
                raw_plan=nyc_plan,
                policy=policy,
                catalog=catalog,
                bindings=bindings,
                connection=connection,
                expect_masked_tail=False,
            ),
        ]
    finally:
        connection.close()

    purpose_missing = json.loads(json.dumps(bts_plan))
    purpose_missing["request_context"]["purpose"] = None
    bad_snapshot = json.loads(json.dumps(bts_plan))
    bad_snapshot["operators"][0]["snapshot"] = "v2099-unavailable"
    public_denied = json.loads(json.dumps(bts_plan))
    public_denied["request_context"]["subject"]["role"] = "public"
    negative = [
        _check_negative_case(
            case_id="REAL-NEG-MASK-REUSE",
            raw_plan=illegal_mask,
            policy=policy,
            catalog=catalog,
            expected_status=ValidationStatus.REJECT,
            expected_reason=ReasonCode.MASKED_FIELD_USED_SEMANTICALLY,
        ),
        _check_negative_case(
            case_id="REAL-NEG-PURPOSE-MISSING",
            raw_plan=purpose_missing,
            policy=policy,
            catalog=catalog,
            expected_status=ValidationStatus.CLARIFY,
            expected_reason=ReasonCode.PURPOSE_MISSING,
        ),
        _check_negative_case(
            case_id="REAL-NEG-SNAPSHOT",
            raw_plan=bad_snapshot,
            policy=policy,
            catalog=catalog,
            expected_status=ValidationStatus.REJECT,
            expected_reason=ReasonCode.VERSION_UNRESOLVED,
        ),
        _check_negative_case(
            case_id="REAL-NEG-PUBLIC-DENY",
            raw_plan=public_denied,
            policy=policy,
            catalog=catalog,
            expected_status=ValidationStatus.REJECT,
            expected_reason=ReasonCode.POLICY_DENIED,
        ),
    ]

    payload = {
        "schema_version": 1,
        "run_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "100K governed semantic smoke; no paper performance timings",
        "status": "PASS",
        "verified_execution_artifacts": [asdict(item) for item in artifact_bindings],
        "governed_cases": [asdict(item) for item in governed],
        "negative_cases": [asdict(item) for item in negative],
    }
    _atomic_json(root / "data/manifests/processed/real-data-governed-smoke.json", payload)
    return payload
