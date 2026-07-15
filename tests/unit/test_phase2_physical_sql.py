"""Tests for the bounded Phase 2 approved-strategy SQL compiler."""

from __future__ import annotations

import pytest

from trustaero.experiments.phase2a import build_phase2_experiment_plan
from trustaero.experiments.physical_sql import (
    compile_phase2_strategy,
    supported_phase2_filter_orders,
    supported_phase2_materialization_targets,
)
from trustaero.experiments.synthetic import (
    SyntheticDataConfig,
    generate_synthetic_workload,
)
from trustaero.ir.models import PhysicalStrategySpec
from trustaero.planner import generate_duckdb_candidates
from trustaero.planner.physical import plan_physical_execution


def test_all_reviewed_boundaries_preserve_rows() -> None:
    """Every legal storage boundary must preserve the governed query result."""

    duckdb = pytest.importorskip("duckdb")
    logical = build_phase2_experiment_plan()
    candidates = generate_duckdb_candidates(
        logical,
        materialization_targets=supported_phase2_materialization_targets(),
    )
    connection = duckdb.connect(":memory:")
    try:
        generate_synthetic_workload(
            connection,
            SyntheticDataConfig(
                workload_id="sql-equivalence",
                row_count=1000,
                temporal_selectivity=0.6,
                spatial_selectivity=0.5,
                policy_selectivity=0.4,
                join_match_rate=0.8,
                hot_key_fraction=0.1,
                seed=5,
            ),
        )
        results = {
            candidate.strategy.strategy_id: tuple(
                connection.execute(compile_phase2_strategy(candidate)).fetchall()
            )
            for candidate in candidates
        }
    finally:
        connection.close()

    assert len(candidates) == 5
    assert len(set(results.values())) == 1


def test_unknown_boundary_fails_closed() -> None:
    """An approved-plan ID alone cannot authorize an unreviewed SQL mapping."""

    logical = build_phase2_experiment_plan()
    candidate = plan_physical_execution(
        logical,
        backend="duckdb",
        strategy=PhysicalStrategySpec(
            strategy_id="unknown-boundary",
            execution_mode="materialized",
            materialize_after=("op-events",),
        ),
    )

    with pytest.raises(ValueError, match="Unsupported Phase 2"):
        compile_phase2_strategy(candidate)


def test_source_lineage_plan_has_executable_instrumentation() -> None:
    """Phase 2B can price real source evidence without enabling record lineage."""

    logical = build_phase2_experiment_plan(source_lineage=True)
    candidates = generate_duckdb_candidates(logical)

    assert logical.lineage_requirements[0].level.value == "source"
    assert candidates[0].unimplemented_backend_features == ()


def test_all_reviewed_filter_orders_preserve_rows_and_physical_order() -> None:
    """Every bounded permutation is equivalent and visible in DuckDB's tree."""

    duckdb = pytest.importorskip("duckdb")
    logical = build_phase2_experiment_plan(source_lineage=True)
    candidates = generate_duckdb_candidates(
        logical,
        filter_orders=supported_phase2_filter_orders(),
    )
    connection = duckdb.connect(":memory:")
    try:
        generate_synthetic_workload(
            connection,
            SyntheticDataConfig(
                workload_id="ordered-equivalence",
                row_count=1000,
                temporal_selectivity=0.6,
                spatial_selectivity=0.5,
                policy_selectivity=0.4,
                join_match_rate=0.8,
                hot_key_fraction=0.1,
                seed=7,
            ),
        )
        results = [
            tuple(connection.execute(compile_phase2_strategy(candidate)).fetchall())
            for candidate in candidates
        ]
        plans = [
            connection.execute(
                f"EXPLAIN (FORMAT JSON) {compile_phase2_strategy(candidate)}"
            ).fetchone()[1]
            for candidate in candidates
        ]
    finally:
        connection.close()

    assert len(candidates) == 7
    assert len(set(results)) == 1
    assert len(set(plans)) == len(candidates)
