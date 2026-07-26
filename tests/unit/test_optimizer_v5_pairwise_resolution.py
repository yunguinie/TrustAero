"""Tests for bounded V5 connection-isolated pairwise resolution."""

from __future__ import annotations

from collections import Counter

from trustaero.experiments.optimizer_v5_pairwise_resolution import (
    balanced_pair_orders,
    classify_pair_ratio,
    merge_pairwise_labels,
)


def test_pair_orders_balance_both_positions_deterministically() -> None:
    first = balanced_pair_orders("fused", "materialized", 40, seed=7)
    second = balanced_pair_orders("fused", "materialized", 40, seed=7)
    assert first == second
    assert Counter(first) == {
        ("fused", "materialized"): 20,
        ("materialized", "fused"): 20,
    }


def test_pair_ci_classification_preserves_three_percent_band() -> None:
    assert classify_pair_ratio(0.80, 0.90, tie_fraction=0.03) == "MATERIALLY_FASTER"
    assert classify_pair_ratio(1.10, 1.20, tie_fraction=0.03) == "MATERIALLY_SLOWER"
    assert classify_pair_ratio(0.98, 1.02, tie_fraction=0.03) == "PRACTICALLY_EQUIVALENT"
    assert classify_pair_ratio(0.96, 1.01, tie_fraction=0.03) == "INCONCLUSIVE"


def _claim(candidate: str, conclusion: str, authorized: bool) -> dict[str, object]:
    return {
        "candidate_id": candidate,
        "baseline_id": "fused",
        "conclusion": conclusion,
        "claim_authorized": authorized,
    }


def test_merge_replaces_only_inconclusive_claims_and_applies_profiles() -> None:
    v2 = {
        "legacy_stability_diagnostics": {
            "observations": [
                {
                    "unit_id": "bts-n100000",
                    "policy_profile": "output-mask-only",
                    "feasible_candidate_ids": [
                        "fused",
                        "materialize-after-bts-filter",
                        "materialize-after-gov-002-mask",
                    ],
                },
                {
                    "unit_id": "bts-n100000",
                    "policy_profile": "no-raw-sensitive-materialization",
                    "feasible_candidate_ids": [
                        "fused",
                        "materialize-after-gov-002-mask",
                    ],
                },
                {
                    "unit_id": "bts-n500000",
                    "policy_profile": "output-mask-only",
                    "feasible_candidate_ids": [
                        "fused",
                        "materialize-after-bts-filter",
                        "materialize-after-gov-002-mask",
                    ],
                },
                {
                    "unit_id": "nyc_tlc-n100000",
                    "policy_profile": "output-mask-only",
                    "feasible_candidate_ids": ["fused", "a", "b"],
                },
                {
                    "unit_id": "nyc_tlc-n500000",
                    "policy_profile": "output-mask-only",
                    "feasible_candidate_ids": [
                        "fused",
                        "materialize-after-nyc-filter",
                        "materialize-after-nyc-zone-join",
                    ],
                },
            ]
        },
        "unit_results": [
            {
                "unit_id": "bts-n100000",
                "paired_claims": [
                    _claim("materialize-after-bts-filter", "INCONCLUSIVE", False),
                    _claim("materialize-after-gov-002-mask", "INCONCLUSIVE", False),
                ],
            },
            {
                "unit_id": "bts-n500000",
                "paired_claims": [
                    _claim("materialize-after-bts-filter", "MATERIALLY_SLOWER", True),
                    _claim("materialize-after-gov-002-mask", "INCONCLUSIVE", False),
                ],
            },
            {
                "unit_id": "nyc_tlc-n100000",
                "paired_claims": [
                    _claim("a", "MATERIALLY_SLOWER", True),
                    _claim("b", "MATERIALLY_SLOWER", True),
                ],
            },
            {
                "unit_id": "nyc_tlc-n500000",
                "paired_claims": [
                    _claim("materialize-after-nyc-filter", "MATERIALLY_SLOWER", True),
                    _claim("materialize-after-nyc-zone-join", "INCONCLUSIVE", False),
                ],
            },
        ],
    }
    pairs = [
        {
            "unit_id": "bts-n100000",
            "paired_claim": _claim("materialize-after-bts-filter", "MATERIALLY_SLOWER", True),
        },
        {
            "unit_id": "bts-n100000",
            "paired_claim": _claim(
                "materialize-after-gov-002-mask", "PRACTICALLY_EQUIVALENT", True
            ),
        },
        {
            "unit_id": "bts-n500000",
            "paired_claim": _claim("materialize-after-gov-002-mask", "MATERIALLY_FASTER", True),
        },
        {
            "unit_id": "nyc_tlc-n500000",
            "paired_claim": _claim("materialize-after-nyc-zone-join", "MATERIALLY_SLOWER", True),
        },
    ]
    result = merge_pairwise_labels(v2, pairs, minimum_model_eligible_units=2)
    assert result["status"] == "PASS_V5_PAIRWISE_LABEL_GATE", result
    assert result["model_eligible_unit_count"] == 4
    strict_bts = next(
        item
        for item in result["profile_labels"]
        if item["unit_id"] == "bts-n100000"
        and item["policy_profile"] == "no-raw-sensitive-materialization"
    )
    assert strict_bts["authorized_oracle_set"] == [
        "fused",
        "materialize-after-gov-002-mask",
    ]
