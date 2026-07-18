"""Tests for grouped Phase 2K loading, fitting, and evaluation."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from trustaero.experiments.pipeline_optimizer import (
    PipelineMaskFamilyObservation,
    cross_validate_pipeline_mask_cost,
    fit_pipeline_mask_cost_model,
    load_pipeline_mask_families,
)
from trustaero.optimizer.mask import MaskPlacementFeatures


def _write_run(path: Path, *, run_id: str, seed: int, rows: int = 100_000) -> Path:
    path.mkdir()
    (path / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "complete",
                "all_validations_passed": True,
                "unit_count": 1,
                "result_equivalent_fragment_count": 1,
                "distinct_physical_plan_fragment_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (path / "environment.json").write_text(
        json.dumps({"commit_hash": f"commit-{run_id}"}), encoding="utf-8"
    )
    fields = [
        "unit_id",
        "benchmark",
        "row_count",
        "identifier_width",
        "match_rate",
        "seed",
        "component",
        "median_latency_ms",
    ]
    common = {
        "unit_id": f"unit-{seed}",
        "benchmark": "mask_fragment",
        "row_count": rows,
        "identifier_width": 256,
        "match_rate": 1.0,
        "seed": seed,
    }
    with (path / "component_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            [
                {
                    **common,
                    "component": "early_mask_fragment",
                    "median_latency_ms": 90.0,
                },
                {
                    **common,
                    "component": "late_mask_fragment",
                    "median_latency_ms": 100.0,
                },
            ]
        )
    return path


def _families() -> list[PipelineMaskFamilyObservation]:
    output = []
    for index, (rows, width, match) in enumerate(
        (
            (50_000, 128, 0.1),
            (50_000, 512, 1.0),
            (100_000, 128, 0.5),
            (100_000, 512, 0.9),
            (150_000, 256, 0.5),
            (150_000, 1024, 1.0),
            (200_000, 128, 0.9),
            (200_000, 512, 1.0),
        )
    ):
        early = 80.0 + index * 10.0
        late = 100.0 + index * 8.0
        output.append(
            PipelineMaskFamilyObservation(
                family_id=f"family-{index}",
                source_run_ids=("run",),
                source_commit_hashes=("commit",),
                seed_count=3,
                features=MaskPlacementFeatures(
                    join_input_rows=rows,
                    identifier_width_bytes=width,
                    join_match_rate=match,
                ),
                median_early_latency_ms=early,
                median_late_latency_ms=late,
                observed_log_early_late_ratio=math.log(early / late),
                tie_threshold_fraction=0.03,
            )
        )
    return output


def test_loader_merges_same_physical_family_across_frozen_runs(tmp_path: Path) -> None:
    runs = [
        _write_run(tmp_path / "run-a", run_id="a", seed=1),
        _write_run(tmp_path / "run-b", run_id="b", seed=2),
    ]
    runs.extend(
        _write_run(
            tmp_path / f"extra-{index}",
            run_id=f"x{index}",
            seed=10 + index,
            rows=110_000 + index * 10_000,
        )
        for index in range(6)
    )

    families = load_pipeline_mask_families(runs)
    merged = next(item for item in families if item.features.join_input_rows == 100_000)

    assert merged.seed_count == 2
    assert merged.source_run_ids == ("a", "b")


def test_fit_is_nonnegative_and_cross_validation_holds_out_whole_family() -> None:
    families = _families()
    model = fit_pipeline_mask_cost_model(families)
    rows = cross_validate_pipeline_mask_cost(families)

    assert all(value >= 0.0 for value in model.coefficients)
    guarded = [
        row
        for row in rows
        if row["evaluation_scheme"] == "pipeline_cost_guarded_leave_one_family_out"
    ]
    assert len(guarded) == len(families)
    assert {row["holdout_family_id"] for row in guarded} == {item.family_id for item in families}
