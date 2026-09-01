"""Fail-closed record lineage for a small row-identity-preserving fragment.

The first implementation intentionally supports one source and excludes Join
and Aggregate.  Every returned row must retain a unique, unmasked source key.
The key itself is never stored in the lineage artifact: both output and source
record identities are salted by their context and hashed.  An independent
checker can recompute the artifact from the actual execution rows, so a
certificate digest cannot certify itself.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from trustaero.ir.enums import DataType, LineageLevel, ReasonCode
from trustaero.ir.models import (
    Diagnostic,
    LineageCapture,
    LineageEvidenceSummary,
    Mask,
    Operator,
    ScanSource,
    ValidatedLogicalPlan,
)

from .lineage import LineageInstrumentationError

_SUPPORTED_OPERATOR_TYPES = {
    "ScanSource",
    "Filter",
    "SpatialFilter",
    "TemporalFilter",
    "Project",
    "Sort",
    "Mask",
    "GeneralizeLocation",
}


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordLineageCaptureSpec:
    """Bind output key columns to the one supported source relation."""

    dataset: str
    key_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.dataset:
            raise ValueError("Record-lineage dataset must be nonempty")
        if not self.key_columns or len(self.key_columns) != len(set(self.key_columns)):
            raise ValueError("Record-lineage key columns must be nonempty and unique")


@dataclass(frozen=True, slots=True)
class RecordLineageEdge:
    """One output-record to source-record edge with no raw key disclosure."""

    output_record_id: str
    source_dataset: str
    source_snapshot: str
    source_record_id: str

    def payload(self) -> dict[str, str]:
        return {
            "output_record_id": self.output_record_id,
            "source_dataset": self.source_dataset,
            "source_snapshot": self.source_snapshot,
            "source_record_id": self.source_record_id,
        }


@dataclass(frozen=True, slots=True)
class RecordLineageArtifact:
    """External evidence artifact referenced by the execution certificate."""

    execution_id: str
    result_id: str
    logical_plan_digest: str
    target_operator: str
    output_row_count: int
    edges: tuple[RecordLineageEdge, ...]

    def payload(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "result_id": self.result_id,
            "logical_plan_digest": self.logical_plan_digest,
            "target_operator": self.target_operator,
            "output_row_count": self.output_row_count,
            "edges": [edge.payload() for edge in self.edges],
        }


@dataclass(frozen=True, slots=True)
class RecordLineageCaptureResult:
    """Observed record evidence, its artifact, and measured capture cost."""

    evidence: LineageEvidenceSummary
    artifact: RecordLineageArtifact
    lineage_digest: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class CompactRecordLineageArtifact:
    """Columnar binary edge artifact with shared source context in one header."""

    execution_id: str
    result_id: str
    logical_plan_digest: str
    target_operator: str
    source_dataset: str
    source_snapshot: str
    output_row_count: int
    edge_bytes: bytes
    encoding: str = "trustaero-record-edges-v2"

    @property
    def edge_count(self) -> int:
        return len(self.edge_bytes) // 64

    def header_payload(self) -> dict[str, object]:
        return {
            "encoding": self.encoding,
            "execution_id": self.execution_id,
            "result_id": self.result_id,
            "logical_plan_digest": self.logical_plan_digest,
            "target_operator": self.target_operator,
            "source_dataset": self.source_dataset,
            "source_snapshot": self.source_snapshot,
            "output_row_count": self.output_row_count,
            "edge_count": self.edge_count,
        }

    def binary_payload(self) -> bytes:
        header = json.dumps(
            self.header_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return len(header).to_bytes(4, "big") + header + self.edge_bytes


@dataclass(frozen=True, slots=True)
class CompactRecordLineageCaptureResult:
    """Compact observed record evidence and measured capture cost."""

    evidence: LineageEvidenceSummary
    artifact: CompactRecordLineageArtifact
    lineage_digest: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class OrdinalRecordLineageArtifact:
    """V4 source identities ordered by their bound result-row ordinal.

    The output side of edge ``i`` is implicitly ``(result_id, i)``.  Because
    ``result_id`` commits to every visible value and row order, V4 needs to
    store only the 32-byte source identity for each output row.
    """

    execution_id: str
    result_id: str
    logical_plan_digest: str
    target_operator: str
    source_dataset: str
    source_snapshot: str
    output_row_count: int
    source_id_bytes: bytes
    encoding: str = "trustaero-record-ordinal-edges-v4"

    @property
    def edge_count(self) -> int:
        return len(self.source_id_bytes) // 32

    def header_payload(self) -> dict[str, object]:
        return {
            "encoding": self.encoding,
            "output_identity": "result-id-and-zero-based-ordinal",
            "execution_id": self.execution_id,
            "result_id": self.result_id,
            "logical_plan_digest": self.logical_plan_digest,
            "target_operator": self.target_operator,
            "source_dataset": self.source_dataset,
            "source_snapshot": self.source_snapshot,
            "output_row_count": self.output_row_count,
            "edge_count": self.edge_count,
        }

    def binary_payload(self) -> bytes:
        header = json.dumps(
            self.header_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return len(header).to_bytes(4, "big") + header + self.source_id_bytes


@dataclass(frozen=True, slots=True)
class OrdinalRecordLineageCaptureResult:
    """Ordinal-bound evidence, artifact, and measured capture latency."""

    evidence: LineageEvidenceSummary
    artifact: OrdinalRecordLineageArtifact
    lineage_digest: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class CompiledRecordLineagePlan:
    """A compiled query that must be executed through the evidence-producing API."""

    plan: ValidatedLogicalPlan
    query: Any
    spec: RecordLineageCaptureSpec


@dataclass(frozen=True, slots=True)
class RecordLineageExecutionResult:
    """Database result and record evidence produced by one indivisible call."""

    query_result: Any
    lineage: RecordLineageCaptureResult


@dataclass(frozen=True, slots=True)
class CompactRecordLineageExecutionResult:
    """Database result and compact record evidence from one indivisible call."""

    query_result: Any
    lineage: CompactRecordLineageCaptureResult


@dataclass(frozen=True, slots=True)
class DatabaseDigestRecordLineageExecutionResult:
    """Visible query result plus DuckDB-assisted compact record evidence."""

    query_result: Any
    lineage: CompactRecordLineageCaptureResult


@dataclass(frozen=True, slots=True)
class OrdinalRecordLineageExecutionResult:
    """Visible query result plus the V4 ordinal-bound record artifact."""

    query_result: Any
    lineage: OrdinalRecordLineageCaptureResult


@dataclass(frozen=True, slots=True)
class RecordLineageArtifactVerification:
    """Independent verification result for an external record-lineage artifact."""

    diagnostics: tuple[Diagnostic, ...]

    @property
    def satisfied(self) -> bool:
        return not self.diagnostics


def _record_target(plan: ValidatedLogicalPlan) -> str:
    requirements = plan.lineage_requirements
    if len(requirements) != 1 or requirements[0].level != LineageLevel.RECORD:
        raise LineageInstrumentationError(
            "Record-lineage V1 requires exactly one record-level target."
        )
    requirement = requirements[0]
    matching = [
        item
        for item in plan.lineage_instrumentation
        if item.target_operator == requirement.target_operator and item.level == LineageLevel.RECORD
    ]
    if len(matching) != 1:
        raise LineageInstrumentationError(
            "Record-lineage V1 requires one matching physical instrumentation spec."
        )
    return requirement.target_operator


def _reachable_fragment(
    plan: ValidatedLogicalPlan,
    target_operator: str,
) -> tuple[Operator, ...]:
    operators = {operator.operator_id: operator for operator in plan.operators}
    found: dict[str, Operator] = {}

    def visit(operator_id: str) -> None:
        if operator_id in found:
            return
        operator = operators.get(operator_id)
        if operator is None:
            raise LineageInstrumentationError(
                f"Record-lineage target references unknown operator: {operator_id}"
            )
        found[operator_id] = operator
        for input_id in operator.inputs:
            visit(input_id)

    visit(target_operator)
    unsupported = sorted(
        {
            operator.operator_type
            for operator in found.values()
            if operator.operator_type not in _SUPPORTED_OPERATOR_TYPES
        }
    )
    if unsupported:
        raise LineageInstrumentationError(
            "Record-lineage V1 does not support operators: " + ", ".join(unsupported)
        )
    return tuple(found.values())


def _identity_context(
    plan: ValidatedLogicalPlan,
    columns: Sequence[str],
    spec: RecordLineageCaptureSpec,
) -> tuple[str, str, tuple[str, ...], tuple[int, ...]]:
    """Validate the fragment and locate identity columns at the trust boundary."""

    target = _record_target(plan)
    fragment = _reachable_fragment(plan, target)
    scans = [operator for operator in fragment if isinstance(operator, ScanSource)]
    if len(scans) != 1 or scans[0].dataset != spec.dataset:
        raise LineageInstrumentationError(
            "Record-lineage V1 requires exactly one matching source dataset."
        )
    snapshot = plan.bindings.data_snapshots.get(spec.dataset)
    if not snapshot:
        raise LineageInstrumentationError(
            f"Record-lineage source has no resolved snapshot: {spec.dataset}"
        )
    if any(
        set(operator.fields).intersection(spec.key_columns)
        for operator in fragment
        if isinstance(operator, Mask)
    ):
        raise LineageInstrumentationError(
            "Record-lineage V1 cannot use a masked field as a source identity."
        )

    column_names = tuple(columns)
    if len(column_names) != len(set(column_names)):
        raise LineageInstrumentationError("Execution output contains duplicate column names.")
    missing = sorted(set(spec.key_columns).difference(column_names))
    if missing:
        raise LineageInstrumentationError(
            "Execution output is missing record-identity columns: " + ", ".join(missing)
        )
    indexes = tuple(column_names.index(column) for column in spec.key_columns)
    return target, snapshot, column_names, indexes


def _derive_artifact(
    plan: ValidatedLogicalPlan,
    *,
    execution_id: str,
    result_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
) -> tuple[RecordLineageArtifact, str]:
    if not execution_id or not result_id:
        raise LineageInstrumentationError(
            "Record-lineage execution and result identifiers must be nonempty."
        )
    target, snapshot, column_names, indexes = _identity_context(plan, columns, spec)

    seen_keys: set[tuple[Any, ...]] = set()
    edges: list[RecordLineageEdge] = []
    for row in rows:
        if len(row) != len(column_names):
            raise LineageInstrumentationError(
                "Execution row width does not match the declared output columns."
            )
        key = tuple(row[index] for index in indexes)
        if any(value is None for value in key):
            raise LineageInstrumentationError(
                "Record-lineage source identities cannot contain NULL."
            )
        if key in seen_keys:
            raise LineageInstrumentationError(
                "Record-lineage V1 requires a unique source identity per output row."
            )
        seen_keys.add(key)
        key_payload = {
            "columns": spec.key_columns,
            "values": key,
        }
        edges.append(
            RecordLineageEdge(
                output_record_id=_digest(
                    {
                        "result_id": result_id,
                        "output_key": key_payload,
                    }
                ),
                source_dataset=spec.dataset,
                source_snapshot=snapshot,
                source_record_id=_digest(
                    {
                        "dataset": spec.dataset,
                        "snapshot": snapshot,
                        "source_key": key_payload,
                    }
                ),
            )
        )

    ordered_edges = tuple(sorted(edges, key=lambda edge: edge.output_record_id))
    artifact = RecordLineageArtifact(
        execution_id=execution_id,
        result_id=result_id,
        logical_plan_digest=plan.validation.canonical_digest,
        target_operator=target,
        output_row_count=len(rows),
        edges=ordered_edges,
    )
    edge_digest = _digest([edge.payload() for edge in ordered_edges])
    return artifact, edge_digest


def _derive_compact_artifact(
    plan: ValidatedLogicalPlan,
    *,
    execution_id: str,
    result_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
) -> tuple[CompactRecordLineageArtifact, str]:
    """Derive fixed-width binary edges without retaining per-edge objects."""

    if not execution_id or not result_id:
        raise LineageInstrumentationError(
            "Record-lineage execution and result identifiers must be nonempty."
        )
    target, snapshot, column_names, indexes = _identity_context(plan, columns, spec)
    source_context = hashlib.sha256(
        json.dumps(
            {
                "dataset": spec.dataset,
                "snapshot": snapshot,
                "columns": spec.key_columns,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()
    result_context = hashlib.sha256(result_id.encode()).digest()
    seen_keys: set[tuple[Any, ...]] = set()
    encoded_edges = bytearray()
    for row in rows:
        if len(row) != len(column_names):
            raise LineageInstrumentationError(
                "Execution row width does not match the declared output columns."
            )
        key = tuple(row[index] for index in indexes)
        if any(value is None for value in key):
            raise LineageInstrumentationError(
                "Record-lineage source identities cannot contain NULL."
            )
        if key in seen_keys:
            raise LineageInstrumentationError(
                "Record-lineage V1 requires a unique source identity per output row."
            )
        seen_keys.add(key)
        # Type tags prevent a string key "1" from colliding with integer key 1.
        key_bytes = json.dumps(
            [
                {
                    "type": f"{type(value).__module__}.{type(value).__qualname__}",
                    "value": value,
                }
                for value in key
            ],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        encoded_edges.extend(hashlib.sha256(b"O" + result_context + key_bytes).digest())
        encoded_edges.extend(hashlib.sha256(b"S" + source_context + key_bytes).digest())

    edge_bytes = bytes(encoded_edges)
    artifact = CompactRecordLineageArtifact(
        execution_id=execution_id,
        result_id=result_id,
        logical_plan_digest=plan.validation.canonical_digest,
        target_operator=target,
        source_dataset=spec.dataset,
        source_snapshot=snapshot,
        output_row_count=len(rows),
        edge_bytes=edge_bytes,
    )
    edge_digest = "sha256:" + hashlib.sha256(edge_bytes).hexdigest()
    return artifact, edge_digest


def capture_record_lineage(
    plan: ValidatedLogicalPlan,
    *,
    execution_id: str,
    result_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
) -> RecordLineageCaptureResult:
    """Capture record edges from actual output rows in the supported fragment."""

    started = time.perf_counter()
    artifact, edge_digest = _derive_artifact(
        plan,
        execution_id=execution_id,
        result_id=result_id,
        columns=columns,
        rows=rows,
        spec=spec,
    )
    evidence = LineageEvidenceSummary(
        execution_id=execution_id,
        result_id=result_id,
        lineage_level=LineageLevel.RECORD,
        covered_operators=(artifact.target_operator,),
        edge_digest=edge_digest,
    )
    return RecordLineageCaptureResult(
        evidence=evidence,
        artifact=artifact,
        lineage_digest=_digest(artifact.payload()),
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )


def capture_compact_record_lineage(
    plan: ValidatedLogicalPlan,
    *,
    execution_id: str,
    result_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
) -> CompactRecordLineageCaptureResult:
    """Capture the same V1 semantics in a fixed-width columnar edge encoding."""

    started = time.perf_counter()
    artifact, edge_digest = _derive_compact_artifact(
        plan,
        execution_id=execution_id,
        result_id=result_id,
        columns=columns,
        rows=rows,
        spec=spec,
    )
    evidence = LineageEvidenceSummary(
        execution_id=execution_id,
        result_id=result_id,
        lineage_level=LineageLevel.RECORD,
        covered_operators=(artifact.target_operator,),
        edge_digest=edge_digest,
    )
    binary = artifact.binary_payload()
    return CompactRecordLineageCaptureResult(
        evidence=evidence,
        artifact=artifact,
        lineage_digest="sha256:" + hashlib.sha256(binary).hexdigest(),
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )


def compile_record_lineage_plan(
    plan: ValidatedLogicalPlan,
    catalog: Any,
    table_bindings: Any,
    *,
    spec: RecordLineageCaptureSpec,
) -> CompiledRecordLineagePlan:
    """Compile only the reviewed row-identity-preserving record fragment.

    The ordinary compiler continues to reject record lineage. This explicit
    entry point checks the fragment, lowers the relationally pass-through
    LineageCapture node, and returns a wrapper that the paired execution API
    consumes together with evidence capture.
    """

    from trustaero.execution.compiler import compile_validated_plan

    # Validate the logical fragment before lowering its pass-through marker.
    target = _record_target(plan)
    _reachable_fragment(plan, target)
    lowered_operators = tuple(
        operator.model_copy(update={"level": LineageLevel.SOURCE})
        if isinstance(operator, LineageCapture) and operator.level == LineageLevel.RECORD
        else operator
        for operator in plan.operators
    )
    lowered = plan.model_copy(update={"operators": lowered_operators})
    query = compile_validated_plan(lowered, catalog, table_bindings)

    # An empty derivation validates source binding, unmasked identity columns,
    # and the requirement/instrumentation relationship before database work.
    _derive_artifact(
        plan,
        execution_id="record-lineage-compile-preflight",
        result_id="sha256:record-lineage-compile-preflight",
        columns=query.output_fields,
        rows=(),
        spec=spec,
    )
    return CompiledRecordLineagePlan(plan=plan, query=query, spec=spec)


def execute_record_lineage_with_connection(
    compiled: CompiledRecordLineagePlan,
    connection: Any,
    *,
    execution_id: str,
) -> RecordLineageExecutionResult:
    """Execute and capture record evidence without an evidence-free success path."""

    from trustaero.execution.duckdb import execute_with_connection

    result = execute_with_connection(compiled.query, connection)
    lineage = capture_record_lineage(
        compiled.plan,
        execution_id=execution_id,
        result_id=result.result_digest,
        columns=result.columns,
        rows=result.rows,
        spec=compiled.spec,
    )
    return RecordLineageExecutionResult(query_result=result, lineage=lineage)


def execute_compact_record_lineage_with_connection(
    compiled: CompiledRecordLineagePlan,
    connection: Any,
    *,
    execution_id: str,
) -> CompactRecordLineageExecutionResult:
    """Execute and capture the compact record artifact in one call."""

    from trustaero.execution.duckdb import execute_with_connection

    result = execute_with_connection(compiled.query, connection)
    lineage = capture_compact_record_lineage(
        compiled.plan,
        execution_id=execution_id,
        result_id=result.result_digest,
        columns=result.columns,
        rows=result.rows,
        spec=compiled.spec,
    )
    return CompactRecordLineageExecutionResult(query_result=result, lineage=lineage)


def _database_digest_prefixes(
    plan: ValidatedLogicalPlan,
    spec: RecordLineageCaptureSpec,
) -> tuple[str, str]:
    output_prefix = json.dumps(
        {
            "kind": "output-record",
            "logical_plan_digest": plan.validation.canonical_digest,
            "target_operator": _record_target(plan),
            "key_columns": spec.key_columns,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = plan.bindings.data_snapshots[spec.dataset]
    source_prefix = json.dumps(
        {
            "kind": "source-record",
            "dataset": spec.dataset,
            "snapshot": snapshot,
            "key_columns": spec.key_columns,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return output_prefix, source_prefix


def _database_key_digest(prefix: str, value: str) -> bytes:
    encoded = value.encode()
    payload = prefix.encode() + str(len(encoded)).encode() + b":" + encoded
    return hashlib.sha256(payload).digest()


def execute_database_digest_record_lineage_with_connection(
    compiled: CompiledRecordLineagePlan,
    connection: Any,
    *,
    execution_id: str,
) -> DatabaseDigestRecordLineageExecutionResult:
    """Let DuckDB batch hidden identity hashing, then assemble 64-byte edges.

    This V3 path deliberately supports one STRING key. Hidden digest columns
    are removed before the user-visible result and its digest are constructed.
    """

    from trustaero.execution.compiler import CompiledQuery
    from trustaero.execution.duckdb import materialize_query_result

    spec = compiled.spec
    if len(spec.key_columns) != 1:
        raise LineageInstrumentationError(
            "Database-assisted record lineage requires exactly one key column."
        )
    key_column = spec.key_columns[0]
    schema = {field.name: field for field in compiled.plan.output_schema}
    if key_column not in schema or schema[key_column].data_type != DataType.STRING:
        raise LineageInstrumentationError(
            "Database-assisted record lineage currently requires one STRING key."
        )
    output_prefix, source_prefix = _database_digest_prefixes(
        compiled.plan,
        spec,
    )
    quoted_key = '"' + key_column.replace('"', '""') + '"'
    key_sql = f"CAST(base.{quoted_key} AS VARCHAR)"
    encoded_length = f"CAST(octet_length(encode({key_sql})) AS VARCHAR)"
    wrapped = CompiledQuery(
        sql=(
            "SELECT base.*, "
            f"unhex(sha256(? || {encoded_length} || ':' || {key_sql})) "
            'AS "__trustaero_output_record_id", '
            f"unhex(sha256(? || {encoded_length} || ':' || {key_sql})) "
            'AS "__trustaero_source_record_id" '
            f"FROM ({compiled.query.sql}) AS base"
        ),
        parameters=(
            output_prefix,
            source_prefix,
            *compiled.query.parameters,
        ),
        output_fields=(
            *compiled.query.output_fields,
            "__trustaero_output_record_id",
            "__trustaero_source_record_id",
        ),
        logical_plan_id=compiled.query.logical_plan_id,
        logical_plan_digest=compiled.query.logical_plan_digest,
    )

    started = time.perf_counter()
    cursor = connection.execute(wrapped.sql, wrapped.parameters)
    fetched = tuple(tuple(row) for row in cursor.fetchall())
    visible_rows = tuple(row[:-2] for row in fetched)
    result = materialize_query_result(compiled.query.output_fields, visible_rows)
    edge_bytes = bytearray()
    seen_output_ids: set[bytes] = set()
    for row in fetched:
        output_id, source_id = row[-2:]
        if not isinstance(output_id, bytes) or not isinstance(source_id, bytes):
            raise LineageInstrumentationError(
                "DuckDB record-lineage digest columns must be binary."
            )
        if len(output_id) != 32 or len(source_id) != 32:
            raise LineageInstrumentationError(
                "DuckDB record-lineage digests must contain exactly 32 bytes."
            )
        if output_id in seen_output_ids:
            raise LineageInstrumentationError(
                "Database-assisted record lineage requires unique output identities."
            )
        seen_output_ids.add(output_id)
        edge_bytes.extend(output_id)
        edge_bytes.extend(source_id)

    snapshot = compiled.plan.bindings.data_snapshots[spec.dataset]
    artifact = CompactRecordLineageArtifact(
        execution_id=execution_id,
        result_id=result.result_digest,
        logical_plan_digest=compiled.plan.validation.canonical_digest,
        target_operator=_record_target(compiled.plan),
        source_dataset=spec.dataset,
        source_snapshot=snapshot,
        output_row_count=result.row_count,
        edge_bytes=bytes(edge_bytes),
        encoding="trustaero-record-edges-duckdb-v3",
    )
    edge_digest = "sha256:" + hashlib.sha256(artifact.edge_bytes).hexdigest()
    evidence = LineageEvidenceSummary(
        execution_id=execution_id,
        result_id=result.result_digest,
        lineage_level=LineageLevel.RECORD,
        covered_operators=(artifact.target_operator,),
        edge_digest=edge_digest,
    )
    binary = artifact.binary_payload()
    lineage = CompactRecordLineageCaptureResult(
        evidence=evidence,
        artifact=artifact,
        lineage_digest="sha256:" + hashlib.sha256(binary).hexdigest(),
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )
    return DatabaseDigestRecordLineageExecutionResult(
        query_result=result,
        lineage=lineage,
    )


def execute_ordinal_record_lineage_with_connection(
    compiled: CompiledRecordLineagePlan,
    connection: Any,
    *,
    execution_id: str,
) -> OrdinalRecordLineageExecutionResult:
    """Build one 32-byte source identity per result ordinal inside DuckDB.

    V4 is deliberately a new, narrower encoding rather than a silent change to
    V3.  The visible result digest binds all row values and their order; source
    digests are stored in that same order, so output digest duplication is not
    required in every edge.
    """

    from trustaero.execution.compiler import CompiledQuery
    from trustaero.execution.duckdb import materialize_query_result

    spec = compiled.spec
    if len(spec.key_columns) != 1:
        raise LineageInstrumentationError("Ordinal record lineage requires exactly one key column.")
    key_column = spec.key_columns[0]
    schema = {field.name: field for field in compiled.plan.output_schema}
    if key_column not in schema or schema[key_column].data_type != DataType.STRING:
        raise LineageInstrumentationError(
            "Ordinal record lineage currently requires one STRING key."
        )
    _, source_prefix = _database_digest_prefixes(compiled.plan, spec)
    quoted_key = '"' + key_column.replace('"', '""') + '"'
    key_sql = f"CAST(base.{quoted_key} AS VARCHAR)"
    encoded_length = f"CAST(octet_length(encode({key_sql})) AS VARCHAR)"
    wrapped = CompiledQuery(
        sql=(
            "SELECT base.*, "
            f"unhex(sha256(? || {encoded_length} || ':' || {key_sql})) "
            'AS "__trustaero_source_record_id" '
            f"FROM ({compiled.query.sql}) AS base"
        ),
        parameters=(source_prefix, *compiled.query.parameters),
        output_fields=(
            *compiled.query.output_fields,
            "__trustaero_source_record_id",
        ),
        logical_plan_id=compiled.query.logical_plan_id,
        logical_plan_digest=compiled.query.logical_plan_digest,
    )

    started = time.perf_counter()
    cursor = connection.execute(wrapped.sql, wrapped.parameters)
    fetched = tuple(tuple(row) for row in cursor.fetchall())
    visible_rows = tuple(row[:-1] for row in fetched)
    result = materialize_query_result(compiled.query.output_fields, visible_rows)
    source_id_bytes = bytearray()
    seen_source_ids: set[bytes] = set()
    for row in fetched:
        source_id = row[-1]
        if not isinstance(source_id, bytes) or len(source_id) != 32:
            raise LineageInstrumentationError(
                "DuckDB ordinal record-lineage identities must be 32-byte binary values."
            )
        if source_id in seen_source_ids:
            raise LineageInstrumentationError(
                "Ordinal record lineage requires unique source identities."
            )
        seen_source_ids.add(source_id)
        # Byte sequence position is the zero-based output ordinal.
        source_id_bytes.extend(source_id)

    snapshot = compiled.plan.bindings.data_snapshots[spec.dataset]
    artifact = OrdinalRecordLineageArtifact(
        execution_id=execution_id,
        result_id=result.result_digest,
        logical_plan_digest=compiled.plan.validation.canonical_digest,
        target_operator=_record_target(compiled.plan),
        source_dataset=spec.dataset,
        source_snapshot=snapshot,
        output_row_count=result.row_count,
        source_id_bytes=bytes(source_id_bytes),
    )
    edge_digest = "sha256:" + hashlib.sha256(artifact.source_id_bytes).hexdigest()
    evidence = LineageEvidenceSummary(
        execution_id=execution_id,
        result_id=result.result_digest,
        lineage_level=LineageLevel.RECORD,
        covered_operators=(artifact.target_operator,),
        edge_digest=edge_digest,
    )
    binary = artifact.binary_payload()
    lineage = OrdinalRecordLineageCaptureResult(
        evidence=evidence,
        artifact=artifact,
        lineage_digest="sha256:" + hashlib.sha256(binary).hexdigest(),
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )
    return OrdinalRecordLineageExecutionResult(
        query_result=result,
        lineage=lineage,
    )


def verify_record_lineage_artifact(
    plan: ValidatedLogicalPlan,
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
    evidence: LineageEvidenceSummary,
    artifact: RecordLineageArtifact,
) -> RecordLineageArtifactVerification:
    """Recompute record evidence from output rows instead of trusting a digest."""

    try:
        expected_artifact, expected_edge_digest = _derive_artifact(
            plan,
            execution_id=artifact.execution_id,
            result_id=artifact.result_id,
            columns=columns,
            rows=rows,
            spec=spec,
        )
    except LineageInstrumentationError as exc:
        return RecordLineageArtifactVerification(
            (
                Diagnostic(
                    code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                    message=str(exc),
                ),
            )
        )

    diagnostics: list[Diagnostic] = []
    if artifact != expected_artifact:
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="Record-lineage artifact does not match the observed result rows.",
            )
        )
    if (
        evidence.execution_id != artifact.execution_id
        or evidence.result_id != artifact.result_id
        or evidence.lineage_level != LineageLevel.RECORD
        or evidence.covered_operators != (artifact.target_operator,)
        or evidence.edge_digest != expected_edge_digest
    ):
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="Record-lineage evidence summary is not bound to the artifact.",
            )
        )
    return RecordLineageArtifactVerification(tuple(diagnostics))


def verify_compact_record_lineage_artifact(
    plan: ValidatedLogicalPlan,
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
    evidence: LineageEvidenceSummary,
    artifact: CompactRecordLineageArtifact,
) -> RecordLineageArtifactVerification:
    """Independently recompute the compact bytes and all summary bindings."""

    try:
        expected_artifact, expected_edge_digest = _derive_compact_artifact(
            plan,
            execution_id=artifact.execution_id,
            result_id=artifact.result_id,
            columns=columns,
            rows=rows,
            spec=spec,
        )
    except LineageInstrumentationError as exc:
        return RecordLineageArtifactVerification(
            (
                Diagnostic(
                    code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                    message=str(exc),
                ),
            )
        )

    diagnostics: list[Diagnostic] = []
    if artifact != expected_artifact:
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="Compact record-lineage artifact does not match observed rows.",
            )
        )
    if (
        evidence.execution_id != artifact.execution_id
        or evidence.result_id != artifact.result_id
        or evidence.lineage_level != LineageLevel.RECORD
        or evidence.covered_operators != (artifact.target_operator,)
        or evidence.edge_digest != expected_edge_digest
    ):
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="Compact record-lineage summary is not bound to the artifact.",
            )
        )
    return RecordLineageArtifactVerification(tuple(diagnostics))


def verify_database_digest_record_lineage_artifact(
    plan: ValidatedLogicalPlan,
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
    evidence: LineageEvidenceSummary,
    artifact: CompactRecordLineageArtifact,
) -> RecordLineageArtifactVerification:
    """Recompute DuckDB V3 identity digests from the visible result keys."""

    try:
        target, snapshot, column_names, indexes = _identity_context(
            plan,
            columns,
            spec,
        )
        if len(indexes) != 1:
            raise LineageInstrumentationError(
                "Database-assisted verification requires exactly one key column."
            )
        output_prefix, source_prefix = _database_digest_prefixes(plan, spec)
        edge_bytes = bytearray()
        seen: set[str] = set()
        for row in rows:
            if len(row) != len(column_names):
                raise LineageInstrumentationError(
                    "Execution row width does not match output columns."
                )
            value = row[indexes[0]]
            if not isinstance(value, str):
                raise LineageInstrumentationError(
                    "Database-assisted verification requires non-null STRING keys."
                )
            if value in seen:
                raise LineageInstrumentationError(
                    "Database-assisted verification requires unique keys."
                )
            seen.add(value)
            edge_bytes.extend(_database_key_digest(output_prefix, value))
            edge_bytes.extend(_database_key_digest(source_prefix, value))
        expected = CompactRecordLineageArtifact(
            execution_id=artifact.execution_id,
            result_id=artifact.result_id,
            logical_plan_digest=plan.validation.canonical_digest,
            target_operator=target,
            source_dataset=spec.dataset,
            source_snapshot=snapshot,
            output_row_count=len(rows),
            edge_bytes=bytes(edge_bytes),
            encoding="trustaero-record-edges-duckdb-v3",
        )
    except LineageInstrumentationError as exc:
        return RecordLineageArtifactVerification(
            (
                Diagnostic(
                    code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                    message=str(exc),
                ),
            )
        )

    expected_edge_digest = "sha256:" + hashlib.sha256(expected.edge_bytes).hexdigest()
    diagnostics: list[Diagnostic] = []
    if artifact != expected:
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="DuckDB record-lineage artifact does not match visible rows.",
            )
        )
    if (
        evidence.execution_id != artifact.execution_id
        or evidence.result_id != artifact.result_id
        or evidence.lineage_level != LineageLevel.RECORD
        or evidence.covered_operators != (target,)
        or evidence.edge_digest != expected_edge_digest
    ):
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="DuckDB record-lineage summary is not bound to the artifact.",
            )
        )
    return RecordLineageArtifactVerification(tuple(diagnostics))


def verify_ordinal_record_lineage_artifact(
    plan: ValidatedLogicalPlan,
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    spec: RecordLineageCaptureSpec,
    evidence: LineageEvidenceSummary,
    artifact: OrdinalRecordLineageArtifact,
) -> RecordLineageArtifactVerification:
    """Recompute both the visible result binding and ordered source identities."""

    from trustaero.execution.duckdb import materialize_query_result

    diagnostics: list[Diagnostic] = []
    try:
        target, snapshot, column_names, indexes = _identity_context(
            plan,
            columns,
            spec,
        )
        if len(indexes) != 1:
            raise LineageInstrumentationError(
                "Ordinal record-lineage verification requires exactly one key."
            )
        canonical_rows = tuple(tuple(row) for row in rows)
        actual_result = materialize_query_result(tuple(columns), canonical_rows)
        _, source_prefix = _database_digest_prefixes(plan, spec)
        source_id_bytes = bytearray()
        seen: set[str] = set()
        for row in canonical_rows:
            if len(row) != len(column_names):
                raise LineageInstrumentationError(
                    "Execution row width does not match output columns."
                )
            value = row[indexes[0]]
            if not isinstance(value, str):
                raise LineageInstrumentationError(
                    "Ordinal record-lineage verification requires non-null STRING keys."
                )
            if value in seen:
                raise LineageInstrumentationError(
                    "Ordinal record-lineage verification requires unique keys."
                )
            seen.add(value)
            source_id_bytes.extend(_database_key_digest(source_prefix, value))
        expected = OrdinalRecordLineageArtifact(
            execution_id=artifact.execution_id,
            result_id=actual_result.result_digest,
            logical_plan_digest=plan.validation.canonical_digest,
            target_operator=target,
            source_dataset=spec.dataset,
            source_snapshot=snapshot,
            output_row_count=len(canonical_rows),
            source_id_bytes=bytes(source_id_bytes),
        )
    except (LineageInstrumentationError, ValueError) as exc:
        return RecordLineageArtifactVerification(
            (
                Diagnostic(
                    code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                    message=str(exc),
                ),
            )
        )

    expected_edge_digest = "sha256:" + hashlib.sha256(expected.source_id_bytes).hexdigest()
    if artifact != expected:
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message=(
                    "Ordinal record-lineage artifact does not match the bound "
                    "visible result and ordered source identities."
                ),
            )
        )
    if (
        evidence.execution_id != artifact.execution_id
        or evidence.result_id != expected.result_id
        or evidence.lineage_level != LineageLevel.RECORD
        or evidence.covered_operators != (target,)
        or evidence.edge_digest != expected_edge_digest
    ):
        diagnostics.append(
            Diagnostic(
                code=ReasonCode.LINEAGE_EVIDENCE_INCONSISTENT,
                message="Ordinal record-lineage summary is not bound to the result.",
            )
        )
    return RecordLineageArtifactVerification(tuple(diagnostics))
