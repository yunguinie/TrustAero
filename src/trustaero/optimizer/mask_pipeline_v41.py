"""Group-sign stability guard for the bounded Pipeline V4 ranking surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import RealPipelineWorkloadStats
from trustaero.optimizer.mask_pipeline_v4_model import PipelineV4CostModel


@dataclass(frozen=True, slots=True)
class PipelineV41SignEnsemble:
    """Primary model plus complete-group deletion models for sign agreement."""

    primary: PipelineV4CostModel
    group_deletion_models: tuple[PipelineV4CostModel, ...]

    def __post_init__(self) -> None:
        if len(self.group_deletion_models) < 2:
            raise ValueError("V4.1 sign stability requires at least two group models")

    def predictions(self, stats: RealPipelineWorkloadStats) -> tuple[float, ...]:
        return (
            self.primary.predict_log_early_late_ratio(stats),
            *(model.predict_log_early_late_ratio(stats) for model in self.group_deletion_models),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "pipeline_work_group_sign_consensus_v4_1",
            "model_schema_version": 1,
            "primary": self.primary.to_dict(),
            "group_deletion_models": [model.to_dict() for model in self.group_deletion_models],
            "uncertainty_rule": "unanimous_prediction_sign_across_primary_and_group_deletions",
            "magnitude_threshold_used": False,
            "governance_before_ranking": True,
            "uncertain_fallback": "reported_separately_not_model_credit",
        }


@dataclass(frozen=True, slots=True)
class PipelineV41Decision:
    direct_placement: MaskPlacement | None
    conservative_fallback_placement: MaskPlacement
    reason_code: str
    predictions: tuple[float, ...]
    within_support: bool
    direct_model_decision: bool


def choose_mask_placement_v41(
    stats: RealPipelineWorkloadStats,
    model: PipelineV41SignEnsemble,
) -> PipelineV41Decision:
    """Use sign consensus after hard governance, without a fitted gap threshold."""

    early = stats.placement_is_legal(MaskPlacement.EARLY)
    late = stats.placement_is_legal(MaskPlacement.LATE)
    if not early and not late:
        raise ValueError("No legal Mask placement satisfies governance")
    if early and not late:
        return PipelineV41Decision(
            MaskPlacement.EARLY,
            MaskPlacement.EARLY,
            "MASK_V41_LATE_INFEASIBLE",
            (),
            model.primary.is_within_support(stats),
            True,
        )
    if late and not early:
        return PipelineV41Decision(
            MaskPlacement.LATE,
            MaskPlacement.LATE,
            "MASK_V41_EARLY_INFEASIBLE",
            (),
            model.primary.is_within_support(stats),
            True,
        )
    predictions = model.predictions(stats)
    within = all(
        candidate.is_within_support(stats)
        for candidate in (model.primary, *model.group_deletion_models)
    )
    if not within:
        return PipelineV41Decision(
            None,
            MaskPlacement.EARLY,
            "MASK_V41_OUT_OF_SUPPORT",
            predictions,
            False,
            False,
        )
    signs = {prediction < 0.0 for prediction in predictions}
    if len(signs) != 1:
        return PipelineV41Decision(
            None,
            MaskPlacement.EARLY,
            "MASK_V41_GROUP_SIGN_DISAGREEMENT",
            predictions,
            True,
            False,
        )
    placement = MaskPlacement.EARLY if next(iter(signs)) else MaskPlacement.LATE
    return PipelineV41Decision(
        placement,
        MaskPlacement.EARLY,
        "MASK_V41_UNANIMOUS_SIGN_RANKING",
        predictions,
        True,
        True,
    )
