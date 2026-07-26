"""Unit checks for V3 transfer root-cause calculations."""

from __future__ import annotations

import pytest

from trustaero.experiments.optimizer_transfer_diagnosis import (
    v1_early_required_match_width_product,
)
from trustaero.optimizer.mask import MaskOptimizerConfig


def test_v1_boundary_includes_per_row_share_of_fixed_setup() -> None:
    model = MaskOptimizerConfig(
        hashed_identifier_width_bytes=64,
        early_materialization_setup_bytes=256_000,
    )

    assert v1_early_required_match_width_product(1_000, model) == pytest.approx(320.0)


def test_v1_boundary_rejects_empty_join_input() -> None:
    with pytest.raises(ValueError, match="positive"):
        v1_early_required_match_width_product(0)
