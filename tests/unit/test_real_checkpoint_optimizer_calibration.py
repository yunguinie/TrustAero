from __future__ import annotations

import pytest

from trustaero.experiments.real_checkpoint_optimizer_calibration import (
    _learn_query_threshold,
    source_month_group,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
)


def _stats(query_rate: float) -> GovernedCheckpointStatistics:
    return GovernedCheckpointStatistics(
        input_rows=1000,
        sensitive_width_bytes=128.0,
        estimated_policy_rows=300,
        estimated_query_rows=round(1000 * query_rate),
        estimated_result_rows=min(300, round(1000 * query_rate)),
        statistic_provenance="catalog_exact_controlled",
    )


def test_source_month_group_keeps_complete_month_together() -> None:
    assert source_month_group("bts-2024-04-w128-p010-q020") == "bts-2024-04"
    assert source_month_group("nyc_tlc-2024-05-w1024-p050-q040") == "nyc_tlc-2024-05"
    with pytest.raises(ValueError, match="Unknown"):
        source_month_group("synthetic-n150000")


def test_threshold_is_learned_only_from_supplied_training_families() -> None:
    statistics = {
        ("bts-2024-04-low", 1): _stats(0.20),
        ("bts-2024-04-high", 1): _stats(0.40),
        # This excluded family would prefer the opposite label at 0.20.
        ("nyc_tlc-2024-05-excluded", 1): _stats(0.20),
    }
    oracles = {
        "bts-2024-04-low": (QUERY_FIRST_CHECKPOINT,),
        "bts-2024-04-high": (POLICY_FIRST_CHECKPOINT,),
        "nyc_tlc-2024-05-excluded": (POLICY_FIRST_CHECKPOINT,),
    }
    threshold = _learn_query_threshold(
        {"bts-2024-04-low", "bts-2024-04-high"},
        statistics,
        oracles,
        (0.20, 0.30, 0.40),
    )
    assert threshold == 0.30
