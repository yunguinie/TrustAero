"""Preflight tests for the frozen governed pipeline admission."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from trustaero.experiments.governed_pipeline_admission import (
    _analyze,
    _create_data,
    _drop_execution_tables,
    governed_pipeline_admission_units,
    load_governed_pipeline_admission_config,
    validate_declared_candidate_space,
)
from trustaero.experiments.governed_pipeline_execution import (
    build_executable_governed_pipeline,
)
from trustaero.optimizer.governed_pipeline_space import (
    QUERY_FIRST_RAW_CHECKPOINT,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/governed_pipeline_admission_v1_1.json"
ORIGINAL_CONFIG = ROOT / "experiments/configs/governed_pipeline_admission_v1.json"


def test_frozen_matrix_size_and_timing_count() -> None:
    config = load_governed_pipeline_admission_config(CONFIG)
    units = governed_pipeline_admission_units(config)

    assert len(units) == 48
    assert len({unit.scenario_id for unit in units}) == 16
    assert config.measured_blocks_per_unit == 30
    assert len(units) * config.measured_blocks_per_unit == 1440


def test_declared_candidate_space_is_checked_before_timing() -> None:
    validate_declared_candidate_space(load_governed_pipeline_admission_config(CONFIG))
    with pytest.raises(ValueError, match="j1.0"):
        validate_declared_candidate_space(load_governed_pipeline_admission_config(ORIGINAL_CONFIG))


def test_join_match_rate_changes_observed_cardinality() -> None:
    config = load_governed_pipeline_admission_config(CONFIG)
    units = governed_pipeline_admission_units(config)
    low = next(unit for unit in units if unit.join_match_rate == 0.1)
    high = next(
        unit
        for unit in units
        if unit.join_match_rate == 0.5
        and unit.identifier_width == low.identifier_width
        and unit.policy_selectivity == low.policy_selectivity
        and unit.query_selectivity == low.query_selectivity
        and unit.seed == low.seed
    )
    connection = duckdb.connect(":memory:")
    try:
        low_rows = _create_data(connection, low)
        high_rows = _create_data(connection, high)
    finally:
        connection.close()

    assert low_rows["query_join_rows"] < high_rows["query_join_rows"]
    assert low_rows["result_rows"] < high_rows["result_rows"]


def test_admission_cleanup_removes_query_first_intermediate_tables() -> None:
    config = load_governed_pipeline_admission_config(CONFIG)
    unit = governed_pipeline_admission_units(config)[0]
    candidate = build_executable_governed_pipeline(
        QUERY_FIRST_RAW_CHECKPOINT,
        unit.checkpoint_unit,
    )
    connection = duckdb.connect(":memory:")
    try:
        _create_data(connection, unit)
        for statement in candidate.setup_sql:
            connection.execute(statement)
        _drop_execution_tables(connection)
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE table_name IN "
                "('raw_query_checkpoint', 'pipeline_checkpoint')"
            ).fetchall()
        }
    finally:
        connection.close()

    assert remaining == set()


def test_carryover_is_checked_per_seed_unit(monkeypatch) -> None:
    """Repeated block indices from different seeds must never be pooled."""

    config = load_governed_pipeline_admission_config(CONFIG)
    observed_units: list[set[str]] = []

    def fake_carryover(rows, **_kwargs):  # type: ignore[no-untyped-def]
        unit_ids = {str(row["unit_id"]) for row in rows}
        observed_units.append(unit_ids)
        return [
            {
                "classification": "NO_MATERIAL_CARRYOVER",
                "carryover_candidate_id": config.candidate_ids[0],
                "target_candidate_id": config.candidate_ids[1],
            }
        ]

    monkeypatch.setattr(
        "trustaero.experiments.governed_pipeline_admission.assess_carryover",
        fake_carryover,
    )
    rows: list[dict[str, str]] = []
    for seed in (1, 2):
        unit_id = f"scenario-s{seed}"
        for candidate in config.candidate_ids:
            rows.append(
                {
                    "scenario_id": "scenario",
                    "unit_id": unit_id,
                    "seed": str(seed),
                    "block_index": "0",
                    "candidate_id": candidate,
                    "latency_ms": "1.0",
                    "client_materialization_latency_ms": "1.0",
                    "permutation_id": "->".join(config.candidate_ids),
                }
            )

    _analyze(rows, config)

    assert observed_units == [{"scenario-s1"}, {"scenario-s2"}]
