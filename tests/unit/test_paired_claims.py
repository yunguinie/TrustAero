"""Statistical claim gates for paired formal timing blocks."""

from __future__ import annotations

import pytest

from trustaero.experiments.paired_claims import assess_carryover, authorize_paired_claims
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders


def _rows() -> list[dict[str, str | int | float]]:
    candidates = ("fused", "polluter", "alternative")
    rows: list[dict[str, str | int | float]] = []
    for block, order in enumerate(complete_permutation_orders(candidates, 30, seed=17)):
        for position, candidate in enumerate(order):
            latency = {"fused": 100.0, "polluter": 200.0, "alternative": 90.0}[candidate]
            # Model a real carryover hazard: the route immediately after the
            # heavy polluter appears 20% faster because its input is warm.
            if position == 1 and order[0] == "polluter":
                latency *= 0.8
            for repeat in range(5):
                rows.append(
                    {
                        "block_index": block,
                        "candidate_id": candidate,
                        "permutation_id": " -> ".join(order),
                        "client_materialization_latency_ms": latency + repeat * 0.001,
                    }
                )
    return rows


def test_carryover_check_uses_mirrored_middle_position_orders() -> None:
    findings = assess_carryover(
        _rows(),
        candidate_ids=("fused", "polluter", "alternative"),
        carryover_candidate_ids=("polluter",),
        tolerance_fraction=0.1,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=7,
        minimum_pairs=5,
    )

    assert len(findings) == 2
    assert {item["classification"] for item in findings} == {"MATERIAL_CARRYOVER_DETECTED"}
    assert all(
        item["median_exposed_over_control_ratio"] == pytest.approx(0.8, abs=0.0001)
        for item in findings
    )


def test_claims_use_only_pollution_safe_pairs_and_require_ci() -> None:
    claims = authorize_paired_claims(
        _rows(),
        candidate_ids=("fused", "polluter", "alternative"),
        baseline_id="fused",
        carryover_candidate_ids=("polluter",),
        tie_fraction=0.03,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=11,
        minimum_blocks=10,
    )
    by_candidate = {item["candidate_id"]: item for item in claims}

    assert by_candidate["alternative"]["pollution_safe_block_count"] == 10
    assert by_candidate["alternative"]["conclusion"] == "MATERIALLY_FASTER"
    assert by_candidate["alternative"]["claim_authorized"] is True
    assert by_candidate["polluter"]["pollution_safe_block_count"] == 15
    assert by_candidate["polluter"]["conclusion"] == "MATERIALLY_SLOWER"
    assert by_candidate["polluter"]["claim_authorized"] is True


def test_point_estimate_cannot_authorize_an_interval_that_crosses_boundaries() -> None:
    rows = _rows()
    for row in rows:
        if row["candidate_id"] == "alternative":
            block = int(row["block_index"])
            repeat = float(row["client_materialization_latency_ms"]) % 1.0
            row["client_materialization_latency_ms"] = (90.0 if block % 2 == 0 else 110.0) + repeat
    claims = authorize_paired_claims(
        rows,
        candidate_ids=("fused", "polluter", "alternative"),
        baseline_id="fused",
        carryover_candidate_ids=("polluter",),
        tie_fraction=0.03,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=13,
        minimum_blocks=10,
    )
    alternative = next(item for item in claims if item["candidate_id"] == "alternative")

    assert alternative["confidence_interval"]["lower"] < 0.97
    assert alternative["confidence_interval"]["upper"] > 1.03
    assert alternative["conclusion"] == "INCONCLUSIVE"
    assert alternative["claim_authorized"] is False
