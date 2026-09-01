"""Tests for Experiment 3 combinatorial candidate spaces."""

from trustaero.experiments.candidate_space_scalability_exp3 import (
    POLICY_REGIMES,
    SCALE_POINTS,
    generate_candidates,
    is_legal,
)
from trustaero.optimizer.governed_pipeline_space import GovernedPipelineStatistics


def _statistics() -> GovernedPipelineStatistics:
    return GovernedPipelineStatistics(
        input_rows=100_000,
        estimated_policy_rows=40_000,
        estimated_query_rows=25_000,
        estimated_governed_rows=12_000,
        estimated_query_join_rows=18_000,
        estimated_result_rows=8_000,
        sensitive_width_bytes=32.0,
    )


def test_nested_spaces_have_exact_unique_structures() -> None:
    for size in SCALE_POINTS:
        candidates = generate_candidates(_statistics(), size)
        assert len(candidates) == size
        assert len({item.candidate_id for item in candidates}) == size
        assert len({item.fingerprint for item in candidates}) == size


def test_every_scale_has_a_legal_candidate_in_every_regime() -> None:
    for size in SCALE_POINTS:
        candidates = generate_candidates(_statistics(), size)
        for regime in POLICY_REGIMES:
            assert any(is_legal(item, regime) for item in candidates)


def test_policy_boundaries_reduce_the_48_candidate_space() -> None:
    candidates = generate_candidates(_statistics(), 48)
    assert sum(is_legal(item, "permissive") for item in candidates) == 48
    assert sum(is_legal(item, "no_raw_join") for item in candidates) == 24
    assert sum(is_legal(item, "strict") for item in candidates) == 6
