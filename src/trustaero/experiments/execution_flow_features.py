"""Bind controlled EA-0 candidates to the Execution-Aware work contract.

This adapter is intentionally explicit.  It converts trusted, fixed EA-0 SQL
variants into pre-execution active schemas and governance exposure counts; it
does not infer those facts from candidate-authored JSON or from observed
latency.  The resulting vectors can be used by a later calibration runner
without making timing labels part of the optimizer input.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_flow_audit import (
    ExecutionFlowUnit,
    ExecutionFlowVariant,
    execution_flow_variants,
)
from trustaero.optimizer.candidate_feasibility import CandidateExposure
from trustaero.optimizer.execution_aware import (
    ActiveColumn,
    AggregateWorkKind,
    ExecutionAwareCandidateSpec,
    OperatorInputMode,
    derive_execution_aware_work,
)


def execution_flow_candidate_spec(
    unit: ExecutionFlowUnit,
    variant: ExecutionFlowVariant,
) -> ExecutionAwareCandidateSpec:
    """Create one ID-bound work specification from a fixed EA-0 variant."""

    row_id = ActiveColumn("row_id", 8)
    join_key = ActiveColumn("join_key", 8)
    dimension_key = ActiveColumn("dimension_key", 8)
    marker = ActiveColumn("marker", 8)
    raw = ActiveColumn(
        "sensitive_value",
        unit.identifier_width,
        "raw",
        sensitive=True,
    )
    hashed = ActiveColumn("masked_value", 64, "hashed", sensitive=True)
    aggregate_result = (
        ActiveColumn("result_rows", 8),
        ActiveColumn("marker_sum", 16),
        ActiveColumn("digest_or_length", 16),
    )
    matched = unit.matched_rows
    build_rows = min(unit.row_count, 10_000)
    before_mask = variant.mask_placement == "before_join"
    after_mask = variant.mask_placement == "after_join"
    raw_boundary = variant.materialization_boundary == "raw_after_join"
    masked_boundary = variant.materialization_boundary == "masked_before_join"

    scan_columns: tuple[ActiveColumn, ...]
    probe_columns: tuple[ActiveColumn, ...]
    join_output_columns: tuple[ActiveColumn, ...]
    if before_mask:
        scan_columns = (row_id, raw, join_key)
        probe_columns = (row_id, hashed, join_key)
        join_output_columns = (row_id, hashed, marker)
        raw_join_rows = 0
    elif after_mask:
        scan_columns = (row_id, raw, join_key)
        # The Join key and row identifier are always active.  The raw value is
        # attached to the Join output because the downstream Mask consumes it;
        # this is distinct from claiming DuckDB physically copies exact bytes.
        probe_columns = (row_id, join_key)
        join_output_columns = (row_id, raw, marker)
        raw_join_rows = unit.row_count
    elif variant.variant_id == "raw_materialized_aggregate":
        scan_columns = (raw, join_key)
        probe_columns = (join_key,)
        join_output_columns = (raw, marker)
        raw_join_rows = unit.row_count
    else:
        # Both the genuinely key-only query and the dead projection rely on
        # DuckDB pruning the unused sensitive value before physical execution.
        scan_columns = (join_key,)
        probe_columns = (join_key,)
        join_output_columns = (marker,)
        raw_join_rows = 0

    result_columns: tuple[ActiveColumn, ...]
    if variant.output_kind == "masked_rows":
        result_rows = matched
        result_columns = (row_id, hashed, marker)
    else:
        result_rows = 1
        result_columns = aggregate_result

    mask_mode: OperatorInputMode
    if before_mask:
        mask_rows = unit.row_count
        mask_mode = "materialized_input"
    elif after_mask:
        mask_rows = matched
        mask_mode = "materialized_input" if raw_boundary else "fused_expression"
    else:
        mask_rows = 0
        mask_mode = "none"

    aggregate_columns: tuple[ActiveColumn, ...]
    aggregate_mode: OperatorInputMode
    aggregate_kind: AggregateWorkKind
    if variant.aggregation:
        aggregate_rows = matched
        aggregate_columns = join_output_columns
        aggregate_mode = "materialized_input" if raw_boundary else "fused_expression"
        if variant.variant_id == "raw_materialized_aggregate":
            aggregate_kind = "raw_length"
        elif variant.output_kind == "masked_aggregate":
            aggregate_kind = "masked_digest"
        else:
            aggregate_kind = "simple"
    else:
        aggregate_rows = 0
        aggregate_columns = ()
        aggregate_mode = "none"
        aggregate_kind = "none"

    sort_columns: tuple[ActiveColumn, ...]
    sort_mode: OperatorInputMode
    if variant.sorting:
        sort_rows = matched
        sort_columns = (hashed, row_id)
        sort_mode = "materialized_input" if masked_boundary else "fused_expression"
    else:
        sort_rows = 0
        sort_columns = ()
        sort_mode = "none"

    breakers: list[str] = []
    if raw_boundary:
        breakers.append("raw_materialization")
    if masked_boundary:
        breakers.append("masked_materialization")
    if variant.sorting:
        breakers.append("sort")
    if variant.aggregation:
        breakers.append("aggregate")

    return ExecutionAwareCandidateSpec(
        candidate_id=variant.variant_id,
        physical_plan_id=f"ea0:{unit.unit_id}:{variant.variant_id}",
        statistic_provenance="catalog_exact_controlled",
        scan_rows=unit.row_count,
        scan_columns=scan_columns,
        join_build_rows=build_rows,
        join_build_columns=(dimension_key, marker),
        join_probe_rows=unit.row_count,
        join_probe_columns=probe_columns,
        join_output_rows=matched,
        join_output_columns=join_output_columns,
        mask_rows=mask_rows,
        mask_input_columns=(raw,) if mask_rows else (),
        mask_mode=mask_mode,
        raw_materialization_rows=matched if raw_boundary else 0,
        raw_materialization_columns=(raw, marker) if raw_boundary else (),
        masked_materialization_rows=unit.row_count if masked_boundary else 0,
        masked_materialization_columns=(row_id, hashed, join_key) if masked_boundary else (),
        aggregate_input_rows=aggregate_rows,
        aggregate_input_columns=aggregate_columns,
        aggregate_mode=aggregate_mode,
        aggregate_work_kind=aggregate_kind,
        sort_rows=sort_rows,
        sort_key_columns=sort_columns,
        sort_mode=sort_mode,
        result_rows=result_rows,
        result_columns=result_columns,
        pipeline_breaker_kinds=tuple(breakers),
        exposure=CandidateExposure(
            variant.variant_id,
            raw_rows_exposed_to_join=raw_join_rows,
            raw_rows_materialized=matched if raw_boundary else 0,
            masked_rows_materialized=unit.row_count if masked_boundary else 0,
        ),
    )


def export_execution_flow_features(run_dir: Path) -> Path:
    """Export label-free candidate features for one complete EA-0 artifact."""

    run_dir = run_dir.resolve()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_EXECUTION_FLOW_AUDIT":
        raise ValueError("Execution-flow feature export requires a complete passed run")
    with (run_dir / "variant_summary.csv").open(newline="", encoding="utf-8") as handle:
        source_rows = list(csv.DictReader(handle))
    variants = {item.variant_id: item for item in execution_flow_variants()}
    output_rows: list[dict[str, Any]] = []
    observed_keys: set[tuple[str, str]] = set()
    for row in source_rows:
        unit = ExecutionFlowUnit(
            row_count=int(row["row_count"]),
            identifier_width=int(row["identifier_width"]),
            match_rate=float(row["match_rate"]),
            seed=int(row["seed"]),
        )
        variant = variants[row["variant_id"]]
        key = (unit.unit_id, variant.variant_id)
        if key in observed_keys:
            raise ValueError(f"Duplicate EA-0 feature source row: {key}")
        observed_keys.add(key)
        spec = execution_flow_candidate_spec(unit, variant)
        work = derive_execution_aware_work(spec)
        base: dict[str, Any] = {
            "unit_id": unit.unit_id,
            "row_count": unit.row_count,
            "identifier_width": unit.identifier_width,
            "match_rate": unit.match_rate,
            "seed": unit.seed,
            "variant_id": variant.variant_id,
            "equivalence_group": variant.equivalence_group,
            "physical_plan_id": spec.physical_plan_id,
            "raw_rows_exposed_to_join": spec.exposure.raw_rows_exposed_to_join
            if spec.exposure
            else None,
            "raw_rows_materialized": spec.exposure.raw_rows_materialized if spec.exposure else None,
            "masked_rows_materialized": spec.exposure.masked_rows_materialized
            if spec.exposure
            else None,
        }
        base.update(work.as_dict())
        output_rows.append(base)
    expected = int(summary["unit_count"]) * int(summary["variant_count"])
    if len(output_rows) != expected:
        raise ValueError("Execution-flow feature export matrix is incomplete")
    fields: list[str] = []
    for row in output_rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    output_path = run_dir / "execution_aware_features.csv"
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)
    return output_path
