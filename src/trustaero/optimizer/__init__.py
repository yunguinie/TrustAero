"""Governance-aware physical-plan selection for TrustAero."""

from trustaero.optimizer.mask import (
    MaskOptimizerConfig,
    MaskPlacement,
    MaskPlacementDecision,
    MaskPlacementFeatures,
    choose_mask_placement,
)
from trustaero.optimizer.mask_cost import (
    MASK_COST_FEATURE_NAMES,
    DecomposedMaskCostDecision,
    DecomposedMaskCostModel,
    choose_mask_placement_by_cost,
    mask_candidate_cost_features,
)
from trustaero.optimizer.mask_guard import (
    MASK_GUARD_FEATURE_NAMES,
    LocalRegretCalibrationPoint,
    LocalRegretGuardDecision,
    LocalRegretGuardModel,
    choose_mask_placement_with_local_guard,
    mask_guard_feature_vector,
)
from trustaero.optimizer.mask_residual import (
    MASK_RESIDUAL_FEATURE_NAMES,
    RegretAwareMaskResidualDecision,
    RegretAwareMaskResidualModel,
    choose_mask_placement_with_residual,
    mask_residual_feature_vector,
)
from trustaero.optimizer.mask_v2 import (
    MASK_V2_FEATURE_NAMES,
    MaskV2Decision,
    MaskV2Model,
    choose_mask_placement_v2,
    mask_v2_feature_vector,
)

__all__ = [
    "MaskOptimizerConfig",
    "MaskPlacement",
    "MaskPlacementDecision",
    "MaskPlacementFeatures",
    "MASK_V2_FEATURE_NAMES",
    "MaskV2Decision",
    "MaskV2Model",
    "MASK_COST_FEATURE_NAMES",
    "MASK_GUARD_FEATURE_NAMES",
    "MASK_RESIDUAL_FEATURE_NAMES",
    "DecomposedMaskCostDecision",
    "DecomposedMaskCostModel",
    "LocalRegretCalibrationPoint",
    "LocalRegretGuardDecision",
    "LocalRegretGuardModel",
    "RegretAwareMaskResidualDecision",
    "RegretAwareMaskResidualModel",
    "choose_mask_placement",
    "choose_mask_placement_v2",
    "choose_mask_placement_by_cost",
    "choose_mask_placement_with_local_guard",
    "choose_mask_placement_with_residual",
    "mask_candidate_cost_features",
    "mask_guard_feature_vector",
    "mask_v2_feature_vector",
    "mask_residual_feature_vector",
]
