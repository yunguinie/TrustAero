"""Tests for the frozen Experiment 1 selector and regret calculation."""

from trustaero.experiments.tpch_multijoin_aggregate_exp1 import (
    ELIGIBLE_DIMENSION_FIRST,
    GOVERNED_FACT_FIRST,
    PARTIAL_AGGREGATE_FIRST,
)
from trustaero.experiments.tpch_multijoin_aggregate_exp1_holdout import select_candidate


def test_selector_covers_three_mechanism_regions() -> None:
    assert select_candidate(0.002, 0.9) == PARTIAL_AGGREGATE_FIRST
    assert select_candidate(0.008, 0.4) == GOVERNED_FACT_FIRST
    assert select_candidate(0.03, 0.8) == GOVERNED_FACT_FIRST
    assert select_candidate(0.15, 0.15) == ELIGIBLE_DIMENSION_FIRST
    assert select_candidate(0.4, 0.6) == ELIGIBLE_DIMENSION_FIRST
