"""Focused tests for frozen component deletion."""

from trustaero.experiments.final_paper_ablation import _without_prefixes


def test_without_prefixes_zeros_only_requested_cost_family() -> None:
    coefficients = {
        "mask.rows": 2.0,
        "mask.input_bytes": 3.0,
        "join.probe_rows": 5.0,
    }

    result = _without_prefixes(coefficients, ("mask.",))

    assert result == {
        "mask.rows": 0.0,
        "mask.input_bytes": 0.0,
        "join.probe_rows": 5.0,
    }
    # The helper must not mutate the already frozen model dictionary.
    assert coefficients["mask.rows"] == 2.0
