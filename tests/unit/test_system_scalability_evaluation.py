"""Unit tests for frozen system-scalability evidence gates."""

from __future__ import annotations

from trustaero.experiments.system_scalability import LAYER_IDS
from trustaero.experiments.system_scalability_evaluation import (
    SystemScalabilityEvaluationConfig,
    evaluate_unit_measurements,
)


def _config() -> SystemScalabilityEvaluationConfig:
    return SystemScalabilityEvaluationConfig(
        results_dir="results/evaluation",
        measurement_results_dir="results/measurement",
        measurement_config_path="experiments/configs/measurement.json",
        expected_unit_ids=("bts-100000",),
        expected_blocks_per_unit=30,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=19,
        maximum_position_median_spread_fraction=0.3,
        maximum_half_ratio_drift_fraction=0.2,
        maximum_spilled_unit_count=0,
        required_certificate_status="PARTIAL",
    )


def _rows(*, mutate_result: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for block in range(30):
        rotation = block % len(LAYER_IDS)
        order = LAYER_IDS[rotation:] + LAYER_IDS[:rotation]
        for position, layer in enumerate(order):
            complete = layer == "complete_trustaero_with_certificate"
            lineage = layer in {
                "trustaero_with_source_lineage",
                "complete_trustaero_with_certificate",
            }
            rows.append(
                {
                    "unit_id": "bts-100000",
                    "input_rows": 100000,
                    "output_rows": 10,
                    "block_index": block,
                    "order_position": position,
                    "order_id": "->".join(order),
                    "layer_id": layer,
                    "end_to_end_latency_ms": 11.0 if complete else 10.0,
                    "policy_validation_latency_ms": 0.2 if layer != LAYER_IDS[0] else "",
                    "planner_latency_ms": 0.3 if layer != LAYER_IDS[0] else "",
                    "database_execution_latency_ms": 10.0,
                    "lineage_capture_latency_ms": 0.1 if lineage else "",
                    "certificate_verification_latency_ms": 0.1 if complete else "",
                    "result_digest": (
                        "changed" if mutate_result and block == 0 and complete else "same"
                    ),
                    "lineage_edge_digest": "edge" if lineage else "",
                    "certificate_status": "PARTIAL" if complete else "",
                }
            )
    return rows


def test_stable_complete_matrix_passes() -> None:
    finding = evaluate_unit_measurements(
        _rows(),
        _config(),
        profile={"temporary_spill_bytes": 0},
    )

    assert finding["passed"]
    assert finding["paired_block_count"] == 30
    assert finding["median_complete_over_direct_ratio"] == 1.1


def test_result_mismatch_fails_closed() -> None:
    finding = evaluate_unit_measurements(
        _rows(mutate_result=True),
        _config(),
        profile={"temporary_spill_bytes": 0},
    )

    assert not finding["passed"]
    assert not finding["gates"]["one_result_digest"]
