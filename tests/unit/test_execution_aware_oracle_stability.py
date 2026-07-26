from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.execution_aware_oracle_stability import (
    classify_ratio_interval,
    confidence_undominated_set,
    load_oracle_stability_config,
)


def _config_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "experiments/configs/execution_aware_oracle_stability_v1.json"
    )


def test_ratio_interval_requires_full_confidence_beyond_tie_band() -> None:
    assert classify_ratio_interval(0.80, 0.92, 0.03) == "LEFT_MATERIALLY_FASTER"
    assert classify_ratio_interval(1.04, 1.20, 0.03) == "LEFT_MATERIALLY_SLOWER"
    assert classify_ratio_interval(1.01, 1.17, 0.03) == "NO_PRACTICAL_DOMINANCE_AUTHORIZED"


def test_confidence_oracle_keeps_every_non_dominated_candidate() -> None:
    pairwise = [
        {
            "left_candidate_id": "fused",
            "right_candidate_id": "raw",
            "conclusion": "LEFT_MATERIALLY_FASTER",
        },
        {
            "left_candidate_id": "early",
            "right_candidate_id": "fused",
            "conclusion": "NO_PRACTICAL_DOMINANCE_AUTHORIZED",
        },
        {
            "left_candidate_id": "early",
            "right_candidate_id": "raw",
            "conclusion": "NO_PRACTICAL_DOMINANCE_AUTHORIZED",
        },
    ]

    assert confidence_undominated_set(("early", "fused", "raw"), pairwise) == (
        "early",
        "fused",
    )


def test_protocol_rejects_group_cherry_picking_and_weak_bootstrap() -> None:
    config = load_oracle_stability_config(_config_path())

    with pytest.raises(ValueError, match="every deployable group"):
        replace(
            config,
            deployable_equivalence_groups=config.deployable_equivalence_groups[:-1],
        )
    with pytest.raises(ValueError, match="at least 1000"):
        replace(config, bootstrap_draws=999)
