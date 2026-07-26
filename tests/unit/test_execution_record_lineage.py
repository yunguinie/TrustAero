"""Tests for the deliberately small, independently checked record fragment."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from trustaero.execution import (
    LineageInstrumentationError,
    RecordLineageCaptureSpec,
    TableBindings,
    capture_compact_record_lineage,
    capture_record_lineage,
    compile_record_lineage_plan,
    execute_compact_record_lineage_with_connection,
    execute_database_digest_record_lineage_with_connection,
    execute_ordinal_record_lineage_with_connection,
    execute_record_lineage_with_connection,
    verify_compact_record_lineage_artifact,
    verify_database_digest_record_lineage_artifact,
    verify_ordinal_record_lineage_artifact,
    verify_record_lineage_artifact,
)
from trustaero.ir.enums import AggregateFunction, ValidationStatus
from trustaero.ir.models import (
    Aggregate,
    AggregateExpression,
    Mask,
    PolicySet,
)
from trustaero.validator.service import validate


def _record_plan(accept_plan, policy_set, catalog):
    policy_raw = policy_set.model_dump(mode="json")
    policy_raw["rules"][0]["obligations"] = [
        {"obligation_type": "LINEAGE_CAPTURE", "parameters": {"level": "record"}}
    ]
    response = validate(
        copy.deepcopy(accept_plan),
        PolicySet.model_validate(policy_raw),
        catalog,
    )
    assert response.status == ValidationStatus.REWRITE
    assert response.validated_plan is not None
    return response.validated_plan


def test_record_lineage_is_derived_from_actual_unique_output_keys(
    accept_plan, policy_set, catalog
) -> None:
    plan = _record_plan(accept_plan, policy_set, catalog)
    rows = (("us7000", 4.2), ("us7001", 3.8))

    captured = capture_record_lineage(
        plan,
        execution_id="exec-record-1",
        result_id="sha256:result-record-1",
        columns=("event_id", "magnitude"),
        rows=rows,
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )

    assert captured.evidence.lineage_level.value == "record"
    assert captured.evidence.covered_operators == ("op2",)
    assert captured.artifact.output_row_count == 2
    assert len(captured.artifact.edges) == 2
    assert captured.latency_ms >= 0.0
    # The external artifact carries only contextual hashes, never raw keys.
    assert "us7000" not in json.dumps(captured.artifact.payload())

    verification = verify_record_lineage_artifact(
        plan,
        columns=("event_id", "magnitude"),
        rows=rows,
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        evidence=captured.evidence,
        artifact=captured.artifact,
    )
    assert verification.satisfied


def test_explicit_record_execution_entry_point_produces_evidence(
    accept_plan, policy_set, catalog
) -> None:
    import duckdb

    plan = _record_plan(accept_plan, policy_set, catalog)
    compiled = compile_record_lineage_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE earthquake_events(
                event_id VARCHAR,
                event_time TIMESTAMPTZ,
                latitude DOUBLE,
                longitude DOUBLE,
                magnitude DOUBLE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO earthquake_events VALUES
            ('us7000', '2026-06-01T00:00:00Z', 40.0, -120.0, 4.2),
            ('us7001', '2026-06-02T00:00:00Z', 41.0, -121.0, 3.8)
            """
        )
        executed = execute_record_lineage_with_connection(
            compiled,
            connection,
            execution_id="exec-integrated-1",
        )
    finally:
        connection.close()

    assert executed.query_result.row_count == 2
    assert executed.lineage.artifact.output_row_count == 2
    assert executed.lineage.evidence.result_id == executed.query_result.result_digest
    assert verify_record_lineage_artifact(
        plan,
        columns=executed.query_result.columns,
        rows=executed.query_result.rows,
        spec=compiled.spec,
        evidence=executed.lineage.evidence,
        artifact=executed.lineage.artifact,
    ).satisfied


def test_compact_record_execution_entry_point_produces_evidence(
    accept_plan, policy_set, catalog
) -> None:
    import duckdb

    plan = _record_plan(accept_plan, policy_set, catalog)
    compiled = compile_record_lineage_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE earthquake_events AS
            SELECT 'eq-' || CAST(i AS VARCHAR) AS event_id,
                   TIMESTAMPTZ '2026-01-01 00:00:00+00' AS event_time,
                   40.0 AS latitude, -120.0 AS longitude, 4.2 AS magnitude
            FROM range(10) AS source(i)
            """
        )
        executed = execute_compact_record_lineage_with_connection(
            compiled,
            connection,
            execution_id="exec-compact-integrated",
        )
    finally:
        connection.close()

    assert executed.query_result.row_count == 10
    assert executed.lineage.artifact.edge_count == 10
    assert executed.lineage.evidence.result_id == executed.query_result.result_digest


def test_database_digest_record_execution_hides_instrumentation_and_verifies(
    accept_plan, policy_set, catalog
) -> None:
    """DuckDB may compute evidence columns, but callers must only see plan output."""

    import duckdb

    plan = _record_plan(accept_plan, policy_set, catalog)
    compiled = compile_record_lineage_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE earthquake_events(
                event_id VARCHAR,
                event_time TIMESTAMPTZ,
                latitude DOUBLE,
                longitude DOUBLE,
                magnitude DOUBLE
            )
            """
        )
        # Include a multibyte identifier so Python and DuckDB must agree on
        # UTF-8 byte length, not merely the number of characters.
        connection.execute(
            """
            INSERT INTO earthquake_events VALUES
            ('eq-1', '2026-06-01T00:00:00Z', 40.0, -120.0, 4.2),
            ('测试', '2026-06-02T00:00:00Z', 41.0, -121.0, 3.8)
            """
        )
        executed = execute_database_digest_record_lineage_with_connection(
            compiled,
            connection,
            execution_id="exec-database-digest",
        )
    finally:
        connection.close()

    assert executed.query_result.columns == compiled.query.output_fields
    assert executed.query_result.row_count == 2
    assert all(len(row) == len(compiled.query.output_fields) for row in executed.query_result.rows)
    assert executed.lineage.artifact.encoding == "trustaero-record-edges-duckdb-v3"
    assert len(executed.lineage.artifact.edge_bytes) == 2 * 64

    verification = verify_database_digest_record_lineage_artifact(
        plan,
        columns=executed.query_result.columns,
        rows=executed.query_result.rows,
        spec=compiled.spec,
        evidence=executed.lineage.evidence,
        artifact=executed.lineage.artifact,
    )
    assert verification.satisfied

    tampered = replace(
        executed.lineage.artifact,
        edge_bytes=b"\x00" + executed.lineage.artifact.edge_bytes[1:],
    )
    assert not verify_database_digest_record_lineage_artifact(
        plan,
        columns=executed.query_result.columns,
        rows=executed.query_result.rows,
        spec=compiled.spec,
        evidence=executed.lineage.evidence,
        artifact=tampered,
    ).satisfied


def test_ordinal_record_lineage_rejects_all_result_and_artifact_mutations(
    accept_plan, policy_set, catalog
) -> None:
    """V4 saves 32 bytes per edge without weakening result-order binding."""

    import duckdb

    plan = _record_plan(accept_plan, policy_set, catalog)
    compiled = compile_record_lineage_plan(
        plan,
        catalog,
        TableBindings(dataset_tables={"earthquakes": "earthquake_events"}),
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE earthquake_events(
                event_id VARCHAR,
                event_time TIMESTAMPTZ,
                latitude DOUBLE,
                longitude DOUBLE,
                magnitude DOUBLE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO earthquake_events VALUES
            ('eq-1', '2026-06-01T00:00:00Z', 40.0, -120.0, 4.2),
            ('eq-2', '2026-06-02T00:00:00Z', 41.0, -121.0, 3.8)
            """
        )
        executed = execute_ordinal_record_lineage_with_connection(
            compiled,
            connection,
            execution_id="exec-ordinal-v4",
        )
    finally:
        connection.close()

    rows = executed.query_result.rows
    artifact = executed.lineage.artifact
    evidence = executed.lineage.evidence
    assert artifact.edge_count == 2
    assert len(artifact.source_id_bytes) == 64
    assert b"eq-1" not in artifact.binary_payload()
    assert verify_ordinal_record_lineage_artifact(
        plan,
        columns=executed.query_result.columns,
        rows=rows,
        spec=compiled.spec,
        evidence=evidence,
        artifact=artifact,
    ).satisfied

    # Each mutation models a different evidence attack. The checker receives
    # the original certificate summary and must fail closed in every case.
    first, second = artifact.source_id_bytes[:32], artifact.source_id_bytes[32:]
    mutations = (
        {
            "rows": tuple(reversed(rows)),
            "artifact": artifact,
        },
        {
            "rows": rows[:-1],
            "artifact": artifact,
        },
        {
            "rows": (*rows, ("eq-3", 5.0)),
            "artifact": artifact,
        },
        {
            "rows": rows,
            "artifact": replace(artifact, source_id_bytes=second + first),
        },
        {
            "rows": rows,
            "artifact": replace(
                artifact,
                source_id_bytes=b"\x00" + artifact.source_id_bytes[1:],
            ),
        },
        {
            "rows": rows,
            "artifact": replace(artifact, result_id="sha256:forged-result"),
        },
    )
    for mutation in mutations:
        assert not verify_ordinal_record_lineage_artifact(
            plan,
            columns=executed.query_result.columns,
            rows=mutation["rows"],
            spec=compiled.spec,
            evidence=evidence,
            artifact=mutation["artifact"],
        ).satisfied


def test_record_lineage_artifact_cannot_self_certify_tampered_edges(
    accept_plan, policy_set, catalog
) -> None:
    plan = _record_plan(accept_plan, policy_set, catalog)
    captured = capture_record_lineage(
        plan,
        execution_id="exec-record-2",
        result_id="sha256:result-record-2",
        columns=("event_id", "magnitude"),
        rows=(("us7000", 4.2),),
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )
    edge = captured.artifact.edges[0]
    tampered = replace(
        captured.artifact,
        edges=(replace(edge, source_record_id="sha256:tampered"),),
    )

    verification = verify_record_lineage_artifact(
        plan,
        columns=("event_id", "magnitude"),
        rows=(("us7000", 4.2),),
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        evidence=captured.evidence,
        artifact=tampered,
    )

    assert not verification.satisfied
    assert {item.code.value for item in verification.diagnostics} == {
        "LINEAGE_EVIDENCE_INCONSISTENT"
    }


def test_compact_record_lineage_uses_fixed_width_edges_and_detects_tampering(
    accept_plan, policy_set, catalog
) -> None:
    plan = _record_plan(accept_plan, policy_set, catalog)
    rows = tuple((f"eq-{index:04d}", float(index)) for index in range(1_000))
    captured = capture_compact_record_lineage(
        plan,
        execution_id="exec-compact-1",
        result_id="sha256:result-compact-1",
        columns=("event_id", "magnitude"),
        rows=rows,
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
    )

    assert captured.artifact.edge_count == 1_000
    assert len(captured.artifact.edge_bytes) == 64_000
    assert len(captured.artifact.binary_payload()) < 65_000
    assert b"eq-0000" not in captured.artifact.binary_payload()
    assert verify_compact_record_lineage_artifact(
        plan,
        columns=("event_id", "magnitude"),
        rows=rows,
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        evidence=captured.evidence,
        artifact=captured.artifact,
    ).satisfied

    tampered = replace(
        captured.artifact,
        edge_bytes=b"\x00" + captured.artifact.edge_bytes[1:],
    )
    assert not verify_compact_record_lineage_artifact(
        plan,
        columns=("event_id", "magnitude"),
        rows=rows,
        spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        evidence=captured.evidence,
        artifact=tampered,
    ).satisfied


@pytest.mark.parametrize(
    ("columns", "rows", "message"),
    [
        (("magnitude",), ((4.2,),), "missing record-identity"),
        (
            ("event_id", "magnitude"),
            (("duplicate", 4.2), ("duplicate", 3.8)),
            "unique source identity",
        ),
        (("event_id", "magnitude"), ((None, 4.2),), "cannot contain NULL"),
    ],
)
def test_record_lineage_fails_closed_on_invalid_output_identity(
    accept_plan,
    policy_set,
    catalog,
    columns,
    rows,
    message,
) -> None:
    plan = _record_plan(accept_plan, policy_set, catalog)

    with pytest.raises(LineageInstrumentationError, match=message):
        capture_record_lineage(
            plan,
            execution_id="exec-invalid",
            result_id="sha256:result-invalid",
            columns=columns,
            rows=rows,
            spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        )


def test_record_lineage_v1_rejects_aggregate(accept_plan, policy_set, catalog) -> None:
    plan = _record_plan(accept_plan, policy_set, catalog)
    aggregate = Aggregate(
        operator_type="Aggregate",
        operator_id="op2",
        inputs=("op1",),
        group_by=("event_id",),
        aggregates=(
            AggregateExpression(
                function=AggregateFunction.COUNT,
                output_field="event_count",
            ),
        ),
    )
    aggregate_plan = plan.model_copy(
        update={"operators": (plan.operators[0], aggregate, *plan.operators[2:])}
    )

    with pytest.raises(LineageInstrumentationError, match="Aggregate"):
        capture_record_lineage(
            aggregate_plan,
            execution_id="exec-aggregate",
            result_id="sha256:result-aggregate",
            columns=("event_id", "event_count"),
            rows=(("us7000", 2),),
            spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        )


def test_record_lineage_v1_rejects_masked_identity(accept_plan, policy_set, catalog) -> None:
    plan = _record_plan(accept_plan, policy_set, catalog)
    mask = Mask(
        operator_type="Mask",
        operator_id="op2",
        inputs=("op1",),
        fields=("event_id",),
        method="hash",
    )
    masked_plan = plan.model_copy(
        update={"operators": (plan.operators[0], mask, *plan.operators[2:])}
    )

    with pytest.raises(LineageInstrumentationError, match="masked field"):
        capture_record_lineage(
            masked_plan,
            execution_id="exec-mask",
            result_id="sha256:result-mask",
            columns=("event_id", "magnitude"),
            rows=(("hashed-id", 4.2),),
            spec=RecordLineageCaptureSpec("earthquakes", ("event_id",)),
        )
