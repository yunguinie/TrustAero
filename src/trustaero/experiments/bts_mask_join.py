"""Semantic BTS early/late Mask placement over a native airport Join.

The sensitive ``Tail_Number`` is output data, while ``OriginAirportID`` is the
Join key.  This distinction is essential: moving Mask before the Join is safe
only because the Join never interprets the masked field.  The smoke performs
no latency measurement.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_bts_mask_join_slice_artifacts
from trustaero.execution import (
    TableBindings,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_candidates import (
    verify_candidate_execution_certificate,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _semantic_digest
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import Mask, PolicySet, ValidatedLogicalPlan
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

BTS_MASK_JOIN_WORKLOAD = "bts_mask_join"
BTS_MASK_JOIN_TARGET = "bts-mp-project"


def _create_bts_mask_join_views(
    connection: Any,
    data_root: Path,
    *,
    sample_rows: int,
    full_month: bool = False,
) -> TableBindings:
    """Bind only the reviewed native fields used by the placement query."""

    base = data_root / "processed/bts/on_time/2024-01"
    flights = (
        base / "bts_flights_full.parquet"
        if full_month
        else base / f"bts_flights_{sample_rows}.parquet"
    )
    airports = base / "bts_airports.parquet"
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mp_flights AS SELECT "
        "CAST(FlightDate AS TIMESTAMPTZ) AS FlightDate, "
        "CAST(OriginAirportID AS BIGINT) AS OriginAirportID, "
        "CAST(Tail_Number AS VARCHAR) AS Tail_Number, "
        "CAST(Distance AS DOUBLE) AS Distance, "
        "CAST(Cancelled AS BOOLEAN) AS Cancelled "
        f"FROM read_parquet({_sql_literal(flights)})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mp_airports AS SELECT "
        "CAST(airport_id AS BIGINT) AS airport_id, "
        "CAST(airport_code AS VARCHAR) AS airport_code, "
        "CAST(city_name AS VARCHAR) AS city_name, "
        "CAST(state_code AS VARCHAR) AS state_code "
        f"FROM read_parquet({_sql_literal(airports)})"
    )
    return TableBindings(
        dataset_tables={
            "bts_on_time_2024_01_mask_join": "trust_bts_mp_flights",
            "bts_airports_2024_01_mask_join": "trust_bts_mp_airports",
        }
    )


def _filtered_row_count(connection: Any) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM trust_bts_mp_flights "
        "WHERE FlightDate >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00' "
        "AND FlightDate < TIMESTAMPTZ '2024-01-22 00:00:00+00:00' "
        "AND Distance >= 750.0 AND Cancelled = false"
    ).fetchone()
    if row is None:
        raise GovernedRealDataSmokeError("BTS Mask/Join filter count is missing")
    return int(row[0])


def _projection_boundary(plan_json: str) -> dict[str, int]:
    """Summarize observed projection placement around DuckDB's Hash Join."""

    payload = json.loads(plan_json)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise GovernedRealDataSmokeError("Unexpected DuckDB JSON plan shape")

    def projection_count(node: dict[str, Any]) -> int:
        children = node.get("children", [])
        return (1 if node.get("name") == "PROJECTION" else 0) + sum(
            projection_count(child) for child in children if isinstance(child, dict)
        )

    def find_join(
        node: dict[str, Any],
        projections_above: int,
    ) -> tuple[dict[str, Any], int] | None:
        if node.get("name") == "HASH_JOIN":
            return node, projections_above
        next_above = projections_above + (1 if node.get("name") == "PROJECTION" else 0)
        for child in node.get("children", []):
            if isinstance(child, dict) and (found := find_join(child, next_above)) is not None:
                return found
        return None

    found = find_join(payload[0], 0)
    if found is None:
        raise GovernedRealDataSmokeError("DuckDB plan has no Hash Join")
    join, above = found
    children = join.get("children", [])
    if not isinstance(children, list) or len(children) != 2 or not isinstance(children[0], dict):
        raise GovernedRealDataSmokeError("DuckDB Hash Join does not have two inputs")
    return {
        "projection_nodes_above_join": above,
        "projection_nodes_in_fact_subtree": projection_count(children[0]),
    }


def run_bts_mask_join_smoke(
    project_root: Path,
    *,
    sample_rows: int = 100_000,
) -> dict[str, Any]:
    """Execute and independently compare legal early/late Mask routes."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for BTS Mask/Join") from exc

    root = project_root.resolve()
    artifacts = verify_bts_mask_join_slice_artifacts(root / "data", sample_rows)
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_mask_join_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_mask_join_policy.json"))
    response = validate(
        _load_json(examples / "plans/bts_mask_join_placement.json"),
        policy,
        catalog,
    )
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("BTS Mask/Join plan did not validate as expected")
    logical: ValidatedLogicalPlan = response.validated_plan
    masks = [operator for operator in logical.operators if isinstance(operator, Mask)]
    if len(masks) != 1 or masks[0].fields != ("Tail_Number",):
        raise GovernedRealDataSmokeError("BTS Mask/Join has an unexpected Mask contract")
    mask = masks[0]
    candidates = generate_duckdb_candidates(
        logical,
        operator_placements=((mask.operator_id, BTS_MASK_JOIN_TARGET),),
    )
    early_candidate = next(
        candidate
        for candidate in candidates
        if candidate.strategy.execution_mode == "governance_placed"
    )
    early_physical = {
        operator.logical_operator_id: operator for operator in early_candidate.physical_operators
    }
    if (
        early_physical[mask.operator_id].inputs != (f"phys-{BTS_MASK_JOIN_TARGET}",)
        or f"phys-{mask.operator_id}" not in early_physical["bts-mp-airport-join"].inputs
    ):
        raise GovernedRealDataSmokeError("Approved physical DAG did not move Mask before Join")

    connection = duckdb.connect()
    candidate_results: list[dict[str, Any]] = []
    expected_digest: str | None = None
    fingerprints: set[str] = set()
    boundaries: dict[str, dict[str, int]] = {}
    try:
        connection.execute("SET threads = 4")
        bindings = _create_bts_mask_join_views(
            connection,
            root / "data",
            sample_rows=sample_rows,
        )
        filtered_rows = _filtered_row_count(connection)
        exposures = tuple(
            CandidateExposure(
                candidate_id=candidate.strategy.strategy_id,
                raw_rows_exposed_to_join=(
                    filtered_rows if candidate.strategy.execution_mode == "fused" else 0
                ),
                raw_rows_materialized=0,
                masked_rows_materialized=0,
            )
            for candidate in candidates
        )
        feasibility = {
            profile.policy_id: filter_feasible_candidates(exposures, profile)
            for profile in (
                GovernanceFeasibilityPolicy("raw-join-permitted", None, 0),
                GovernanceFeasibilityPolicy("no-raw-sensitive-join", 0, 0),
            )
        }
        for candidate, exposure in zip(candidates, exposures, strict=True):
            compiled = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(compiled, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError("Early and late Mask results differ")
            tail_index = execution.columns.index("Tail_Number")
            raw_output_rows = sum(
                value is not None
                and (
                    len(str(value)) != 64
                    or any(character not in "0123456789abcdef" for character in str(value))
                )
                for value in (row[tail_index] for row in execution.rows)
            )
            if raw_output_rows:
                raise GovernedRealDataSmokeError("A Mask/Join route exposed raw Tail_Number")
            observation = observe_duckdb_plan(
                connection,
                compiled.sql,
                compiled.parameters,
                analyze=False,
            )
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError("Mask placement collapsed in DuckDB")
            fingerprints.add(observation.fingerprint)
            strategy_id = candidate.strategy.strategy_id
            boundaries[strategy_id] = _projection_boundary(observation.plan_json)
            certificate_status = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"bts-mask-join-{strategy_id}",
            )
            candidate_results.append(
                {
                    "strategy_id": strategy_id,
                    "execution_mode": candidate.strategy.execution_mode,
                    "physical_plan_id": candidate.physical_plan_id,
                    "output_row_count": execution.row_count,
                    "semantic_result_digest": digest,
                    "duckdb_plan_fingerprint": observation.fingerprint,
                    "duckdb_operator_names": list(observation.operator_names),
                    "observed_projection_boundary": boundaries[strategy_id],
                    "certificate_status": certificate_status,
                    "exposure": asdict(exposure),
                }
            )
    finally:
        connection.close()

    fused = boundaries["fused"]
    early_id = next(item for item in boundaries if item != "fused")
    early = boundaries[early_id]
    observed_boundary_direction = (
        early["projection_nodes_in_fact_subtree"] > fused["projection_nodes_in_fact_subtree"]
        and early["projection_nodes_above_join"] < fused["projection_nodes_above_join"]
    )
    # Tiny unit fixtures may be fully projection-folded by DuckDB. The real
    # 100K smoke must still expose the expected physical boundary direction.
    if sample_rows >= 100_000 and not observed_boundary_direction:
        raise GovernedRealDataSmokeError("Observed DuckDB Mask boundary did not move before Join")
    strict = feasibility["no-raw-sensitive-join"]
    if strict.feasible_candidate_ids != (early_id,) or strict.rejected_candidate_ids != ("fused",):
        raise GovernedRealDataSmokeError("Strict raw-Join policy did not force early Mask")

    payload = {
        "schema_version": 1,
        "status": "PASS",
        "purpose": "BTS native Mask/Join placement semantic smoke; no timing",
        "paper_performance_evidence": False,
        "sample_rows": sample_rows,
        "filtered_rows_entering_join": filtered_rows,
        "verified_execution_artifacts": [asdict(item) for item in artifacts],
        "candidate_count": len(candidates),
        "distinct_duckdb_plan_count": len(fingerprints),
        "candidates": candidate_results,
        "governance_profiles": {
            name: {
                "status": result.status,
                "feasible_candidate_ids": list(result.feasible_candidate_ids),
                "rejected_candidate_ids": list(result.rejected_candidate_ids),
                "decisions": [asdict(item) for item in result.decisions],
            }
            for name, result in feasibility.items()
        },
        "strict_policy_forces_early_mask": True,
        "approved_dag_places_mask_before_join": True,
        "duckdb_boundary_direction_observed": observed_boundary_direction,
    }
    _atomic_json(
        root / "data/manifests/processed/bts-mask-join-semantic-smoke.json",
        payload,
    )
    return payload
