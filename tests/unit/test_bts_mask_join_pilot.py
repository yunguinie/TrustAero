"""Tests for the paired BTS Mask/Join timing protocol and its gates."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from trustaero.experiments.bts_mask_join_analysis import analyze_bts_mask_join_pilot
from trustaero.experiments.bts_mask_join_pilot import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    MASK_JOIN_PILOT_LABEL,
    BtsMaskJoinPilotConfig,
)


def _config(**overrides: object) -> BtsMaskJoinPilotConfig:
    values: dict[str, object] = {
        "results_dir": "results/test-mask-join",
        "sample_rows": 100_000,
        "warmup_blocks": 2,
        "measured_blocks": 4,
        "duckdb_threads": 1,
        "duckdb_memory_limit_mb": 512,
        "order_seed": 7,
        "absolute_half_drift_limit": 0.50,
        "paired_ratio_half_drift_limit": 0.20,
        "paired_ratio_outlier_fraction_limit": 0.10,
        "tie_threshold_fraction": 0.03,
        "query_family_protocol_sha256": "a" * 64,
        "semantic_smoke_sha256": "b" * 64,
        "require_clean_git": False,
        "scientific_label": MASK_JOIN_PILOT_LABEL,
    }
    values.update(overrides)
    return BtsMaskJoinPilotConfig(**values)  # type: ignore[arg-type]


def test_mask_join_config_requires_complete_pair_permutations() -> None:
    with pytest.raises(ValueError, match="measured blocks"):
        _config(measured_blocks=3)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _config(semantic_smoke_sha256="not-a-digest")
    with pytest.raises(ValueError, match="frozen 547271"):
        _config(full_month=True)

    full = _config(full_month=True, sample_rows=547_271)
    assert full.full_month is True


def test_mask_join_analysis_accepts_balanced_stable_pairs(tmp_path: Path) -> None:
    config = _config()
    (tmp_path / "config.json").write_text(
        json.dumps(asdict(config)),
        encoding="utf-8",
    )
    (tmp_path / "environment.json").write_text(
        json.dumps({"git_dirty": True}),
        encoding="utf-8",
    )
    candidate_template = {
        "certificate_status": "PARTIAL",
        "peak_buffer_memory_bytes": 1024,
        "peak_temp_directory_bytes": 0,
    }
    summary = {
        "run_id": "test-run",
        "status": "PASS",
        "sample_rows": 100_000,
        "full_month": False,
        "scientific_label": MASK_JOIN_PILOT_LABEL,
        "paper_performance_evidence": False,
        "optimizer_selection_evaluated": False,
        "candidate_count": 2,
        "distinct_duckdb_plan_count": 2,
        "verified_execution_artifacts": [{}, {}],
        "candidate_summaries": {
            LATE_CANDIDATE: {
                **candidate_template,
                "median_ms": 10.0,
                "p95_ms": 10.0,
            },
            EARLY_CANDIDATE: {
                **candidate_template,
                "median_ms": 9.5,
                "p95_ms": 9.5,
            },
        },
        "governance_profiles": {
            "no-raw-sensitive-join": {
                "feasible_candidate_ids": [EARLY_CANDIDATE],
            }
        },
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    rows = []
    orders = (
        (LATE_CANDIDATE, EARLY_CANDIDATE),
        (EARLY_CANDIDATE, LATE_CANDIDATE),
    )
    for block_index in range(4):
        order = orders[block_index % 2]
        for position, candidate in enumerate(order):
            rows.append(
                {
                    "block_index": block_index,
                    "permutation_id": " -> ".join(order),
                    "order_position": position,
                    "candidate_id": candidate,
                    "client_materialization_latency_ms": (
                        9.5 if candidate == EARLY_CANDIDATE else 10.0
                    ),
                }
            )
    with (tmp_path / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    result = analyze_bts_mask_join_pilot(tmp_path)

    assert result["status"] == "PASS"
    assert result["paired_protocol_stable_for_future_clean_run"] is True
    assert result["formal_paper_experiment_authorized"] is False
    assert result["median_early_over_late_ratio"] == pytest.approx(0.95)
    assert result["diagnostic_oracle_set_within_tie_band"] == [EARLY_CANDIDATE]
