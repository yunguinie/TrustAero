"""Semantic smoke tests for the three-candidate checkpoint family."""

from __future__ import annotations

import hashlib

import duckdb

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.governed_checkpoint_multicandidate import (
    FUSED,
    MULTICANDIDATE_IDS,
    build_governed_checkpoint_candidate,
    checkpoint_candidate_feasibility,
    execute_governed_checkpoint_candidate,
)
from trustaero.experiments.governed_checkpoint_reversal import (
    POLICY_FIRST,
    QUERY_FIRST,
    GovernedCheckpointUnit,
    _create_data,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy


def _unit() -> GovernedCheckpointUnit:
    return GovernedCheckpointUnit(2_000, 128, 0.25, 0.4, 17)


def test_three_candidates_are_result_equivalent_and_physically_distinct() -> None:
    """A new candidate enters timing only after semantic and physical checks."""

    connection = duckdb.connect(":memory:")
    try:
        unit = _unit()
        actual = _create_data(connection, unit)
        digests: set[str] = set()
        fingerprints: set[str] = set()
        for candidate_id in MULTICANDIDATE_IDS:
            candidate = build_governed_checkpoint_candidate(
                candidate_id,
                unit,
                actual,
            )
            # Profile outside a timing sample.  Materialized candidates have
            # two pipeline phases; fused has one.
            phase_fingerprints: list[str] = []
            if candidate.checkpoint_sql is not None:
                checkpoint = observe_duckdb_plan(
                    connection,
                    candidate.checkpoint_sql,
                    analyze=False,
                )
                phase_fingerprints.append(checkpoint.fingerprint)
                # DuckDB may materialize DDL targets while explaining a CREATE
                # statement.  Profiling must not leak state into execution.
                connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
                connection.execute(candidate.checkpoint_sql)
            output = observe_duckdb_plan(
                connection,
                candidate.output_sql,
                analyze=False,
            )
            phase_fingerprints.append(output.fingerprint)
            fingerprints.add(hashlib.sha256("|".join(phase_fingerprints).encode()).hexdigest())
            checksum = execute_governed_checkpoint_candidate(connection, candidate)
            digests.add(str(checksum))

        assert len(digests) == 1
        assert len(fingerprints) == 3
    finally:
        connection.close()


def test_governance_profiles_change_the_legal_candidate_set() -> None:
    connection = duckdb.connect(":memory:")
    try:
        unit = _unit()
        actual = _create_data(connection, unit)
    finally:
        connection.close()

    permissive = checkpoint_candidate_feasibility(
        unit,
        actual,
        GovernanceFeasibilityPolicy("permissive", None, None),
    )
    no_raw_checkpoint = checkpoint_candidate_feasibility(
        unit,
        actual,
        GovernanceFeasibilityPolicy("no-raw-checkpoint", None, 0),
    )
    required_checkpoint = checkpoint_candidate_feasibility(
        unit,
        actual,
        GovernanceFeasibilityPolicy(
            "checkpoint-required",
            None,
            None,
            require_governance_checkpoint=True,
        ),
    )
    strict = checkpoint_candidate_feasibility(
        unit,
        actual,
        GovernanceFeasibilityPolicy(
            "narrow-checkpoint-required",
            None,
            0,
            require_governance_checkpoint=True,
        ),
    )

    assert permissive.feasible_candidate_ids == MULTICANDIDATE_IDS
    assert no_raw_checkpoint.feasible_candidate_ids == (FUSED, POLICY_FIRST)
    assert required_checkpoint.feasible_candidate_ids == (POLICY_FIRST, QUERY_FIRST)
    assert strict.feasible_candidate_ids == (POLICY_FIRST,)


def test_candidate_metadata_does_not_hide_raw_materialization() -> None:
    unit = _unit()
    actual = {"policy_rows": 500, "query_rows": 800, "result_rows": 200}
    fused = build_governed_checkpoint_candidate(FUSED, unit, actual)
    policy = build_governed_checkpoint_candidate(POLICY_FIRST, unit, actual)
    query = build_governed_checkpoint_candidate(QUERY_FIRST, unit, actual)

    assert fused.checkpoint_sql is None
    assert fused.exposure.provides_governance_checkpoint is False
    assert policy.exposure.raw_rows_materialized == 0
    assert query.exposure.raw_rows_materialized == 800
