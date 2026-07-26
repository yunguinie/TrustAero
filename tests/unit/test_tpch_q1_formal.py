"""Controls for the frozen three-candidate TPC-H Q1 timing protocol."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_governed import GovernedRealDataSmokeError
from trustaero.experiments.tpch_q1_formal import (
    TPCH_Q1_SF10_FORMAL_LABEL,
    TPCH_Q1_SF10_PAIRED_CI_LABEL,
    TpchQ1Timing,
    _completed_measurement_blocks,
    _LineProgress,
    analyze_tpch_q1_formal,
    load_tpch_q1_formal_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_q1_progress_is_newline_delimited(capsys: pytest.CaptureFixture[str]) -> None:
    progress = _LineProgress(total=10, enabled=True)
    for index in range(5):
        progress.advance(f"step-{index}")

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert lines[-1].startswith("[Q1 005/10  50.0%]")


def test_q1_formal_config_freezes_semantics_utc_and_batch_shape() -> None:
    config = load_tpch_q1_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_q1_utc_batched_v1.json"
    )

    assert config.timed_repeats_per_position == 5
    assert config.duckdb_timezone == "UTC"
    assert config.measured_blocks == 30
    assert "q1_decimal" in config.semantic_smoke_path
    with pytest.raises(ValueError, match="at least 30"):
        replace(config, measured_blocks=24)
    with pytest.raises(ValueError, match="odd batch"):
        replace(config, timed_repeats_per_position=4)
    with pytest.raises(ValueError, match="inside the project"):
        replace(config, support_audit_path="../outside.json")

    sf10 = replace(
        config,
        scale_factor=10,
        scientific_label=TPCH_Q1_SF10_FORMAL_LABEL,
        results_dir="results/tpch_sf10_q1_formal_v1",
    )
    assert sf10.scale_factor == 10
    with pytest.raises(ValueError, match="reviewed pair"):
        replace(sf10, scale_factor=3)

    paired = load_tpch_q1_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_sf10_q1_paired_ci_v2.json"
    )
    assert paired.scientific_label == TPCH_Q1_SF10_PAIRED_CI_LABEL
    assert paired.carryover_candidate_ids == ("materialize-after-q01-filter",)
    assert paired.bootstrap_repetitions == 20000


def test_q1_schedule_balances_every_order_and_position() -> None:
    candidates = ("fused", "after-filter", "after-aggregate")
    orders = complete_permutation_orders(candidates, 30, seed=20260721)

    assert len(set(orders)) == 6
    assert set(orders.count(order) for order in set(orders)) == {5}
    for candidate in candidates:
        positions = [order.index(candidate) for order in orders]
        assert positions.count(0) == positions.count(1) == positions.count(2) == 10


def test_q1_resume_accepts_only_complete_atomic_blocks() -> None:
    config = load_tpch_q1_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_q1_utc_batched_v1.json"
    )
    candidates = ("fused", "after-filter", "after-aggregate")
    rows = [
        TpchQ1Timing(
            0,
            "block-0",
            " -> ".join(candidates),
            position,
            repeat,
            candidate,
            "2026-07-20T00:00:00+00:00",
            1.0,
            1.0,
            4,
            "sha256:result",
        )
        for position, candidate in enumerate(candidates)
        for repeat in range(config.timed_repeats_per_position)
    ]

    assert _completed_measurement_blocks(rows, config) == {0}
    with pytest.raises(GovernedRealDataSmokeError, match="incomplete persisted block"):
        _completed_measurement_blocks(rows[:-1], config)


def test_q1_analysis_accepts_balanced_stable_measurements(tmp_path: Path) -> None:
    """Exercise every formal gate without running the 6-million-row database."""

    config = load_tpch_q1_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_q1_utc_batched_v1.json"
    )
    candidates = ("fused", "materialize-after-q01-filter", "materialize-after-q01-aggregate")
    orders = complete_permutation_orders(candidates, 30, seed=config.order_seed + 1)
    rows: list[TpchQ1Timing] = []
    baselines = {
        "fused": 100.0,
        "materialize-after-q01-filter": 120.0,
        "materialize-after-q01-aggregate": 150.0,
    }
    for block, order in enumerate(orders):
        for position, candidate in enumerate(order):
            for repeat in range(5):
                rows.append(
                    TpchQ1Timing(
                        block,
                        f"block-{block}",
                        " -> ".join(order),
                        position,
                        repeat,
                        candidate,
                        "2026-07-20T00:00:00+00:00",
                        baselines[candidate] + repeat * 0.01,
                        baselines[candidate],
                        4,
                        "sha256:result",
                    )
                )
    (tmp_path / "config.json").write_text(json.dumps(asdict(config)), encoding="utf-8")
    (tmp_path / "environment.json").write_text(json.dumps({"git_dirty": False}), encoding="utf-8")
    candidate_summaries = {
        candidate: {
            "certificate_status": "PARTIAL",
            "peak_buffer_memory_bytes": 1024,
            "peak_temp_directory_bytes": 0,
            "median_ms": baseline,
            "p95_ms": baseline,
        }
        for candidate, baseline in baselines.items()
    }
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "fixture",
                "scientific_label": config.scientific_label,
                "timing_protocol": config.timing_protocol,
                "paired_block_statistic": "median_of_5",
                "distinct_duckdb_plan_count": 3,
                "official_result_equivalent_preflight": True,
                "candidate_summaries": candidate_summaries,
            }
        ),
        encoding="utf-8",
    )
    with (tmp_path / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TpchQ1Timing.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)

    result = analyze_tpch_q1_formal(tmp_path)

    assert result["status"] == "PASS"
    assert result["diagnostic_oracle_set_within_tie_band"] == ["fused"]

    # Resuming is useful for completing diagnostics, but a multi-process run
    # cannot claim the frozen single-connection cache protocol.
    (tmp_path / "environment.json").write_text(
        json.dumps(
            {
                "git_dirty": False,
                "execution_segment_count": 2,
                "resumed_run": True,
            }
        ),
        encoding="utf-8",
    )
    resumed = analyze_tpch_q1_formal(tmp_path)
    assert resumed["status"] == "FAIL"
    assert resumed["integrity_gates"]["single_execution_process"] is False


def test_q1_v2_authorizes_only_pollution_safe_ci_claims(tmp_path: Path) -> None:
    """A carryover finding is reported while safe paired claims remain usable."""

    config = load_tpch_q1_formal_config(
        PROJECT_ROOT / "experiments/configs/tpch_sf10_q1_paired_ci_v2.json"
    )
    candidates = ("fused", "materialize-after-q01-filter", "materialize-after-q01-aggregate")
    orders = complete_permutation_orders(candidates, 30, seed=config.order_seed + 1)
    baselines = {
        "fused": 100.0,
        "materialize-after-q01-filter": 200.0,
        "materialize-after-q01-aggregate": 100.5,
    }
    rows: list[TpchQ1Timing] = []
    for block, order in enumerate(orders):
        for position, candidate in enumerate(order):
            latency = baselines[candidate]
            if position == 1 and order[0] == "materialize-after-q01-filter":
                latency *= 0.8
            for repeat in range(5):
                rows.append(
                    TpchQ1Timing(
                        block,
                        f"block-{block}",
                        " -> ".join(order),
                        position,
                        repeat,
                        candidate,
                        "2026-07-20T00:00:00+00:00",
                        latency + repeat * 0.001,
                        latency,
                        4,
                        "sha256:result",
                    )
                )
    (tmp_path / "config.json").write_text(json.dumps(asdict(config)), encoding="utf-8")
    (tmp_path / "environment.json").write_text(
        json.dumps({"git_dirty": False, "execution_segment_count": 1}), encoding="utf-8"
    )
    candidate_summaries = {
        candidate: {
            "certificate_status": "PARTIAL",
            "peak_buffer_memory_bytes": 1024,
            "peak_temp_directory_bytes": 0,
            "median_ms": baseline,
            "p95_ms": baseline,
        }
        for candidate, baseline in baselines.items()
    }
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "paired-ci-fixture",
                "scientific_label": config.scientific_label,
                "timing_protocol": config.timing_protocol,
                "paired_block_statistic": "median_of_5",
                "distinct_duckdb_plan_count": 3,
                "official_result_equivalent_preflight": True,
                "candidate_summaries": candidate_summaries,
            }
        ),
        encoding="utf-8",
    )
    with (tmp_path / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TpchQ1Timing.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)

    result = analyze_tpch_q1_formal(tmp_path)
    claims = {item["candidate_id"]: item for item in result["paired_claims"]}

    assert result["status"] == "PASS"
    assert result["paper_performance_evidence"] is True
    assert {item["classification"] for item in result["carryover_assessments"]} == {
        "MATERIAL_CARRYOVER_DETECTED"
    }
    assert claims["materialize-after-q01-aggregate"]["conclusion"] == ("PRACTICALLY_EQUIVALENT")
    assert claims["materialize-after-q01-aggregate"]["claim_authorized"] is True
    assert claims["materialize-after-q01-filter"]["conclusion"] == "MATERIALLY_SLOWER"
