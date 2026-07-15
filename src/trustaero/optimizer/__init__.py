"""Governance-aware physical-plan selection for TrustAero."""

from trustaero.optimizer.mask import (
    MaskOptimizerConfig,
    MaskPlacement,
    MaskPlacementDecision,
    MaskPlacementFeatures,
    choose_mask_placement,
)

__all__ = [
    "MaskOptimizerConfig",
    "MaskPlacement",
    "MaskPlacementDecision",
    "MaskPlacementFeatures",
    "choose_mask_placement",
]
