from __future__ import annotations

import csv
import itertools
from dataclasses import replace
from pathlib import Path

import pytest

from trustaero.experiments.mask_output_tail_confirmation import (
    MASK_OUTPUT_CANDIDATE_IDS,
    analyze_mask_output_tail,
    load_mask_output_tail_config,
    measurement_orders,
)


def _config_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "experiments/configs/mask_output_tail_confirmation_v1.json"
    )


def test_protocol_has_complete_balanced_permutations() -> None:
    config = load_mask_output_tail_config(_config_path())
    orders = measurement_orders(
        config.candidate_ids,
        config.repetitions_per_permutation,
        seed=config.order_seed,
    )

    assert len(orders) == 30
    assert set(orders) == set(itertools.permutations(MASK_OUTPUT_CANDIDATE_IDS))
    assert all(orders.count(order) == 5 for order in set(orders))
    for candidate_id in config.candidate_ids:
        assert [value for order in orders for value in order].count(candidate_id) == 30
        for position in range(3):
            assert sum(order[position] == candidate_id for order in orders) == 10


def test_protocol_rejects_candidate_cherry_picking() -> None:
    config = load_mask_output_tail_config(_config_path())

    with pytest.raises(ValueError, match="frozen candidate set"):
        replace(config, candidate_ids=config.candidate_ids[:-1])


def test_analysis_uses_paired_seed_clusters_and_preserves_tie(tmp_path: Path) -> None:
    config = replace(
        load_mask_output_tail_config(_config_path()),
        seeds=(11, 22, 33),
        repetitions_per_permutation=5,
        bootstrap_draws=1000,
    )
    rows: list[dict[str, object]] = []
    for seed in config.seeds:
        orders = measurement_orders(
            config.candidate_ids,
            config.repetitions_per_permutation,
            seed=config.order_seed + seed + 1,
        )
        for block, order in enumerate(orders):
            for position, candidate_id in enumerate(order):
                latency = {
                    "prejoin_mask_materialized_output": 101.0,
                    "postjoin_mask_fused_output": 100.0,
                    "postjoin_raw_materialized_mask_output": 99.5,
                }[candidate_id]
                rows.append(
                    {
                        "row_count": config.row_count,
                        "identifier_width": config.identifier_width,
                        "match_rate": config.match_rate,
                        "seed": seed,
                        "unit_id": f"seed-{seed}",
                        "variant_id": candidate_id,
                        "equivalence_group": "mask_output",
                        "repeat_index": block,
                        "order_position": position,
                        "is_warmup": False,
                        "latency_ms": latency,
                        "result_digest": "same",
                        "permutation_id": ">".join(order),
                        "immediate_predecessor_id": "ignored",
                    }
                )
    with (tmp_path / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    result = analyze_mask_output_tail(tmp_path, config)

    assert result["status"] == "PASS_MASK_OUTPUT_TAIL_CONFIRMATION_INTEGRITY"
    assert result["scientific_conclusion"] == "TAIL_DIFFERENCE_NOT_CONFIDENCE_AUTHORIZED"
    assert all(
        item["conclusion"] == "NO_PRACTICAL_DOMINANCE_AUTHORIZED"
        for item in result["pairwise_results"]
    )
