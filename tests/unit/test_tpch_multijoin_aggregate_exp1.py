"""Contract tests for the formal Experiment 1 candidate family."""

import pytest

from trustaero.experiments.tpch_multijoin_aggregate_exp1 import (
    CANDIDATE_IDS,
    ELIGIBLE_DIMENSION_FIRST,
    GOVERNED_FACT_FIRST,
    PARTIAL_AGGREGATE_FIRST,
    WorkloadUnit,
    candidate_sql,
)


def test_three_candidates_cover_distinct_natural_physical_choices() -> None:
    unit = WorkloadUnit(0.25, 0.75, 4103)
    fact_setup, fact_output = candidate_sql(GOVERNED_FACT_FIRST, unit)
    dimension_setup, dimension_output = candidate_sql(ELIGIBLE_DIMENSION_FIRST, unit)
    partial_setup, partial_output = candidate_sql(PARTIAL_AGGREGATE_FIRST, unit)
    assert len(CANDIDATE_IDS) == 3
    assert "governed_lineitem" in fact_setup[0]
    assert "INNER JOIN orders" in fact_output and "INNER JOIN customer" in fact_output
    assert "eligible_orders" in dimension_setup[0]
    assert "AS MATERIALIZED" in dimension_output
    assert "GROUP BY l_orderkey" in partial_setup[0]
    assert "INNER JOIN orders" in partial_output and "INNER JOIN customer" in partial_output


def test_all_candidates_apply_the_same_policy_mask_and_output_contract() -> None:
    unit = WorkloadUnit(0.05, 0.05, 4201)
    compiled = [candidate_sql(candidate, unit) for candidate in CANDIDATE_IDS]
    text = ["\n".join((*setup, output)) for setup, output in compiled]
    assert all("md5(l_comment)" in value for value in text)
    assert all("governed_count" in value for value in text)
    assert all("governed_revenue" in value for value in text)
    assert all("minimum_masked_token" in value for value in text)


def test_unknown_candidate_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unknown Experiment 1 candidate"):
        candidate_sql("invented", WorkloadUnit(0.25, 0.25, 4103))


def test_log_spaced_scenarios_have_distinct_stable_ids() -> None:
    ids = {WorkloadUnit(policy, 0.75, 5101).scenario_id for policy in (0.001, 0.005, 0.01, 0.05)}
    assert ids == {
        "p0.001-q0.750",
        "p0.005-q0.750",
        "p0.010-q0.750",
        "p0.050-q0.750",
    }
