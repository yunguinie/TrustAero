"""Tests for policy-aware compact Phase 2M analysis."""

from __future__ import annotations

from trustaero.experiments.pipeline_ablation_analysis import (
    ABLATION_POLICY_PROFILES,
    _family_policy_rows,
    _unit_policy_rows,
    variant_is_legal,
)


def _component(variant: str, latency: float) -> dict[str, str]:
    exposure = {
        "late_fused": (100, 0, 0),
        "late_join_materialized": (100, 80, 0),
        "late_hash_materialized": (100, 0, 80),
        "early_hash_materialized": (0, 0, 100),
    }[variant]
    return {
        "scenario_id": "family-s1",
        "region_label": "test",
        "row_count": "100",
        "identifier_width": "64",
        "match_rate": "0.8",
        "seed": "1",
        "variant": variant,
        "median_latency_ms": str(latency),
        "raw_rows_exposed_to_join": str(exposure[0]),
        "raw_rows_materialized": str(exposure[1]),
        "masked_rows_materialized": str(exposure[2]),
    }


def test_policy_filters_raw_materialization_before_cost_ranking() -> None:
    raw_materialized = _component("late_join_materialized", 10.0)
    policies = {item.policy_id: item for item in ABLATION_POLICY_PROFILES}

    assert variant_is_legal(raw_materialized, policies["raw_permissive"]) is True
    assert variant_is_legal(raw_materialized, policies["no_raw_materialization"]) is False
    assert variant_is_legal(raw_materialized, policies["no_raw_join"]) is False


def test_unit_analysis_changes_oracle_only_after_legality_filter() -> None:
    rows = [
        _component("late_fused", 30.0),
        _component("late_join_materialized", 10.0),
        _component("late_hash_materialized", 20.0),
        _component("early_hash_materialized", 40.0),
    ]

    results = {row["policy_id"]: row for row in _unit_policy_rows(rows, 0.03)}

    assert results["raw_permissive"]["oracle_fastest_legal_variant"] == ("late_join_materialized")
    assert results["no_raw_materialization"]["oracle_fastest_legal_variant"] == (
        "late_hash_materialized"
    )
    assert results["no_raw_join"]["oracle_fastest_legal_variant"] == ("early_hash_materialized")
    assert results["no_raw_join"]["governance_overhead_percent"] == 300.0
    assert all(row["selected_candidate_is_legal"] for row in results.values())


def test_family_rule_requires_four_of_five_seed_winners() -> None:
    unit_rows = []
    for seed, classification in enumerate(
        (
            "late_hash_materialized",
            "late_hash_materialized",
            "late_hash_materialized",
            "late_hash_materialized",
            "tie",
        )
    ):
        unit_rows.append(
            {
                "family_id": "family",
                "policy_id": "no_raw_materialization",
                "region_label": "test",
                "row_count": 100,
                "identifier_width": 64,
                "match_rate": 0.8,
                "seed": seed,
                "practical_classification": classification,
                "oracle_fastest_legal_variant": "late_hash_materialized",
                "governance_overhead_percent": 10.0,
            }
        )

    family = _family_policy_rows(unit_rows, 0.8)[0]

    assert family["required_seed_agreement_count"] == 4
    assert family["family_classification"] == "late_hash_materialized"
