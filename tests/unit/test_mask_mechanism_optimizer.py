"""Tests for the independently calibrated Mask mechanism formula."""

from __future__ import annotations

import pytest

from trustaero.experiments.mechanism_optimizer import (
    MechanismObservation,
    audit_governance_hard_constraints,
    audit_mechanism_monotonicity,
    cross_validate_mechanism_cost,
    fit_nonnegative_mechanism_cost,
)
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures
from trustaero.optimizer.mask_mechanism import (
    HASH_FEATURE_NAMES,
    JOIN_FEATURE_NAMES,
    MATERIALIZATION_FEATURE_NAMES,
    MechanismMaskCostModel,
    NonnegativeMechanismCost,
    choose_mask_placement_by_mechanism,
)


def _component(
    name: str,
    feature_names: tuple[str, ...],
    coefficients: tuple[float, ...],
) -> NonnegativeMechanismCost:
    return NonnegativeMechanismCost(
        component_name=name,
        feature_names=feature_names,
        intercept_ms=1.0,
        coefficients=coefficients,
        ridge_lambda=0.01,
        training_group_count=8,
        source_run_ids=("run-1",),
    )


def _model() -> MechanismMaskCostModel:
    return MechanismMaskCostModel(
        hash_cost=_component("sha256", HASH_FEATURE_NAMES, (5.0, 2.0)),
        materialization_cost=_component(
            "materialization_roundtrip",
            MATERIALIZATION_FEATURE_NAMES,
            (2.0, 0.5),
        ),
        join_cost=_component("hash_join", JOIN_FEATURE_NAMES, (1.0, 1.0)),
    )


def test_formula_exposes_independent_component_costs() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
    )

    model = _model()
    early = model.candidate_components_ms(features, MaskPlacement.EARLY)
    late = model.candidate_components_ms(features, MaskPlacement.LATE)

    assert set(early) == {"sha256_ms", "payload_movement_ms", "hash_join_ms"}
    assert early["sha256_ms"] > late["sha256_ms"]
    assert early["hash_join_ms"] == late["hash_join_ms"]


def test_governance_constraints_override_lower_estimated_cost() -> None:
    features = MaskPlacementFeatures(
        join_input_rows=100_000,
        identifier_width_bytes=1024,
        join_match_rate=0.1,
        max_raw_exposure_rows=0,
    )

    decision = choose_mask_placement_by_mechanism(features, _model())

    assert decision.placement is MaskPlacement.EARLY
    assert decision.reason_code == "MASK_MECHANISM_LATE_INFEASIBLE"


def test_model_round_trip_and_component_identity_validation() -> None:
    model = _model()

    restored = MechanismMaskCostModel.from_dict(model.to_dict())

    assert restored == model
    with pytest.raises(ValueError, match="incompatible sha256"):
        MechanismMaskCostModel(
            hash_cost=_component("wrong", HASH_FEATURE_NAMES, (1.0, 1.0)),
            materialization_cost=model.materialization_cost,
            join_cost=model.join_cost,
        )


def _fit_observations() -> list[MechanismObservation]:
    output: list[MechanismObservation] = []
    for index, features in enumerate(((1.0, 2.0), (2.0, 1.0), (3.0, 4.0), (4.0, 3.0))):
        # Exact non-negative target: 2 + 3*x0 + 4*x1.
        output.append(
            MechanismObservation(
                component_name="sha256",
                group_id=f"g{index}",
                features=features,
                target_ms=2.0 + 3.0 * features[0] + 4.0 * features[1],
                source_run_id="run-1",
                replicate_count=2,
            )
        )
    return output


def test_nonnegative_fit_and_group_cross_validation() -> None:
    observations = _fit_observations()

    model = fit_nonnegative_mechanism_cost(
        observations, feature_names=HASH_FEATURE_NAMES, ridge_lambda=0.0
    )
    rows = cross_validate_mechanism_cost(
        observations, feature_names=HASH_FEATURE_NAMES, ridge_lambda=0.0
    )

    assert model.intercept_ms == pytest.approx(2.0, abs=1e-6)
    assert model.coefficients == pytest.approx((3.0, 4.0), abs=1e-6)
    assert len(rows) == 4
    assert {row["holdout_group"] for row in rows} == {"g0", "g1", "g2", "g3"}


def test_monotonicity_and_governance_audits_pass_for_nonnegative_formula() -> None:
    model = _model()

    monotonicity = audit_mechanism_monotonicity(
        model,
        row_counts=(100_000, 300_000),
        identifier_widths=(256, 1024),
    )
    governance = audit_governance_hard_constraints(model)

    assert monotonicity["passes"] is True
    assert governance["passes"] is True
