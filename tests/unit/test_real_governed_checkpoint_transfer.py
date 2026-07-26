from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.real_governed_checkpoint_transfer import (
    POLICY_FIRST,
    QUERY_FIRST,
    RealCheckpointProfile,
    RealCheckpointSource,
    RealCheckpointTransferConfig,
    RealCheckpointUnit,
    _create_real_data,
    real_checkpoint_units,
)


def _config() -> RealCheckpointTransferConfig:
    return RealCheckpointTransferConfig(
        results_dir="results/test",
        sources=(
            RealCheckpointSource(
                "bts",
                "2024-02",
                "events.parquet",
                "dimension.parquet",
                "manifest.json",
            ),
        ),
        profiles=(
            RealCheckpointProfile("small", 384, 0.25, 0.08),
            RealCheckpointProfile("large", 640, 0.35, 0.30),
        ),
        row_count=150_000,
        seeds=(8101, 8202, 8303),
        candidate_ids=(POLICY_FIRST, QUERY_FIRST),
        warmup_rounds=1,
        repetitions_per_permutation=15,
        duckdb_threads=1,
        duckdb_memory_limit_mb=512,
        order_seed=1,
        practical_tie_fraction=0.03,
        confidence_level=0.95,
        bootstrap_draws=1000,
        bootstrap_seed=2,
        require_clean_git=False,
    )


def test_matrix_is_complete_and_scenario_excludes_seed() -> None:
    units = real_checkpoint_units(_config())
    assert len(units) == 6
    assert len({unit.scenario_id for unit in units}) == 2
    assert len({unit.unit_id for unit in units}) == 6


def test_development_role_is_explicitly_supported() -> None:
    development = replace(_config(), experiment_role="real_mechanism_development")
    assert development.experiment_role == "real_mechanism_development"


def test_validation_role_is_explicitly_supported() -> None:
    validation = replace(_config(), experiment_role="frozen_real_optimizer_validation")
    assert validation.experiment_role == "frozen_real_optimizer_validation"


def test_final_holdout_role_is_explicitly_supported() -> None:
    holdout = replace(_config(), experiment_role="frozen_real_optimizer_final_holdout")
    assert holdout.experiment_role == "frozen_real_optimizer_final_holdout"


def test_v31_transfer_refuses_scale_drift() -> None:
    payload = _config().__dict__ if hasattr(_config(), "__dict__") else None
    assert payload is None  # slots prevent accidental mutable protocol edits
    with pytest.raises(ValueError, match="150000"):
        RealCheckpointTransferConfig(
            **{
                **{name: getattr(_config(), name) for name in _config().__dataclass_fields__},
                "row_count": 200_000,
            }
        )


def test_create_real_bts_data_preserves_exact_width(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    events = tmp_path / "events.parquet"
    dimension = tmp_path / "dimension.parquet"
    connection.execute(
        """
        CREATE TABLE source_events AS
        SELECT
            DATE '2024-02-01' + CAST(i % 28 AS INTEGER) AS FlightDate,
            'AA' AS Reporting_Airline,
            'N' || CAST(i AS VARCHAR) AS Tail_Number,
            i AS Flight_Number_Reporting_Airline,
            i % 20 AS OriginAirportID,
            'O' AS Origin,
            'D' AS Dest
        FROM range(1600) source(i)
        """
    )
    connection.execute("COPY source_events TO ? (FORMAT PARQUET)", [str(events)])
    connection.execute(
        """
        CREATE TABLE source_dimension AS
        SELECT
            i AS airport_id,
            'A' || CAST(i AS VARCHAR) AS airport_code,
            'City' AS city_name,
            'ST' AS state_code
        FROM range(20) source(i)
        """
    )
    connection.execute("COPY source_dimension TO ? (FORMAT PARQUET)", [str(dimension)])
    unit = RealCheckpointUnit(
        "bts",
        "2024-02",
        events.name,
        dimension.name,
        "test",
        1_500,
        384,
        0.25,
        0.08,
        8101,
    )
    actual = _create_real_data(connection, tmp_path, unit)
    assert actual["input_rows"] == 1_500
    assert actual["query_rows"] == 120
    assert 250 < actual["policy_rows"] < 500
    assert connection.execute(
        "SELECT min(length(sensitive_value)), max(length(sensitive_value)) FROM events"
    ).fetchone() == (384, 384)
    connection.close()


def test_frozen_protocol_is_valid_json() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "experiments/frozen/real_governed_checkpoint_transfer_protocol_v1_20260723.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["optimizer_mutation_allowed"] is False
    assert payload["failure_policy"].startswith("Retain every failed result")
