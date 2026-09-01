"""Tests for Experiment 2 hard-versus-soft analysis."""

from trustaero.experiments.hard_vs_soft_legality_exp2 import (
    evaluate_selection,
    minimum_strict_penalty_ms,
    select_soft_penalty,
)


def test_soft_penalty_crosses_the_legality_boundary() -> None:
    predicted = {"illegal_fast": 5.0, "legal": 12.0}
    legal = ("legal",)
    assert select_soft_penalty(predicted, legal, 0.0) == "illegal_fast"
    assert select_soft_penalty(predicted, legal, 8.0) == "legal"
    assert minimum_strict_penalty_ms(predicted, legal) == 7.0


def test_illegal_selection_never_counts_as_oracle_hit() -> None:
    key = ("scenario", 1, "group")
    metrics = evaluate_selection(
        {key: {"illegal_fast": 5.0, "legal": 12.0}},
        {key: ("legal",)},
        {key: "illegal_fast"},
        0.03,
    )
    assert metrics["illegal_selection_count"] == 1
    assert metrics["within_3_percent_count"] == 0
    assert metrics["legal_selection_count"] == 0
    assert metrics["conditional_mean_regret_percent"] is None


def test_legal_selection_uses_legal_oracle_regret() -> None:
    key = ("scenario", 1, "group")
    metrics = evaluate_selection(
        {key: {"a": 10.0, "b": 10.2, "illegal": 1.0}},
        {key: ("a", "b")},
        {key: "b"},
        0.03,
    )
    assert metrics["illegal_selection_count"] == 0
    assert metrics["within_3_percent_count"] == 1
    assert abs(metrics["conditional_mean_regret_percent"] - 2.0) < 1e-9
