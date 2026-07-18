"""Tests for the Phase 2M complete-pipeline ablation smoke."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from trustaero.experiments.pipeline_ablation import (
    PIPELINE_ABLATION_VARIANTS,
    PipelineAblationConfig,
    PipelineAblationScenario,
    load_pipeline_ablation_config,
    pipeline_ablation_exposure,
    pipeline_ablation_sql,
    run_pipeline_ablation_smoke,
)


def _config(results_dir: Path) -> PipelineAblationConfig:
    return PipelineAblationConfig(
        results_dir=str(results_dir),
        scenarios=(
            PipelineAblationScenario(
                scenario_id="tiny",
                region_label="protocol_test",
                row_count=128,
                identifier_width=32,
                match_rate=0.5,
                seed=1,
            ),
        ),
        warmup_runs=0,
        measured_runs=1,
        profile_runs=1,
        duckdb_threads=1,
        duckdb_memory_limit_mb=512,
        require_clean_git=False,
    )


def test_sql_variants_are_bounded_and_explicit() -> None:
    sql = pipeline_ablation_sql()

    assert tuple(sql) == PIPELINE_ABLATION_VARIANTS
    assert "MATERIALIZED" not in sql["late_fused"]
    assert "joined_events AS MATERIALIZED" in sql["late_join_materialized"]
    assert "masked_events AS MATERIALIZED" in sql["late_hash_materialized"]
    assert "masked_events AS MATERIALIZED" in sql["early_hash_materialized"]
    assert all("HASH_JOIN" not in value for value in sql.values())  # SQL, not plan text.


def test_exposure_annotations_distinguish_raw_and_masked_materialization() -> None:
    scenario = _config(Path("unused")).scenarios[0]

    fused = pipeline_ablation_exposure(scenario, "late_fused")
    raw_materialized = pipeline_ablation_exposure(scenario, "late_join_materialized")
    masked_materialized = pipeline_ablation_exposure(scenario, "late_hash_materialized")
    early = pipeline_ablation_exposure(scenario, "early_hash_materialized")

    assert fused.raw_rows_exposed_to_join == scenario.row_count
    assert raw_materialized.raw_rows_materialized == 64
    assert masked_materialized.masked_rows_materialized == 64
    assert early.raw_rows_exposed_to_join == 0
    assert early.masked_rows_materialized == scenario.row_count


def test_smoke_writes_equivalent_distinct_boundary_validated_artifacts(
    tmp_path: Path,
) -> None:
    pytest.importorskip("duckdb")
    config = _config(tmp_path / "run")

    output = run_pipeline_ablation_smoke(config)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    with (output / "raw_measurements.csv").open(newline="", encoding="utf-8") as handle:
        measurements = list(csv.DictReader(handle))
    with (output / "boundary_summary.csv").open(newline="", encoding="utf-8") as handle:
        boundaries = list(csv.DictReader(handle))

    assert summary["status"] == "complete"
    assert summary["all_validations_passed"] is True
    assert summary["scenario_count"] == 1
    assert summary["variant_count"] == 4
    assert summary["result_equivalent_scenario_count"] == 1
    assert summary["distinct_plan_scenario_count"] == 1
    assert summary["boundary_validated_scenario_count"] == 1
    assert summary["exact_join_cardinality_scenario_count"] == 1
    assert summary["spilled_scenario_count"] == 0
    assert summary["compact_matrix_authorized"] is True
    assert len(measurements) == 4
    assert len({row["result_digest"] for row in measurements}) == 1
    assert len({row["physical_plan_fingerprint"] for row in measurements}) == 4
    assert len(boundaries) == 4

    with (output / "component_summary.csv").open(newline="", encoding="utf-8") as handle:
        components = list(csv.DictReader(handle))
    assert all("raw_rows_materialized" in row for row in components)

    resumed = run_pipeline_ablation_smoke(config, resume_run_id=output.name)
    assert resumed == output


def test_loader_rejects_duplicate_scenario_ids(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    scenario = {
        "scenario_id": "same",
        "region_label": "test",
        "row_count": 10,
        "identifier_width": 8,
        "match_rate": 1.0,
        "seed": 1,
    }
    config_path.write_text(
        json.dumps(
            {
                "results_dir": str(tmp_path / "run"),
                "scenarios": [scenario, scenario],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scenario IDs"):
        load_pipeline_ablation_config(config_path)


def test_loader_expands_templates_without_seed_leakage(tmp_path: Path) -> None:
    config_path = tmp_path / "templates.json"
    config_path.write_text(
        json.dumps(
            {
                "results_dir": str(tmp_path / "run"),
                "scenario_templates": [
                    {
                        "scenario_id_prefix": "family",
                        "region_label": "development",
                        "row_count": 10,
                        "identifier_width": 8,
                        "match_rate": 0.5,
                    }
                ],
                "seeds": [7, 8],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_pipeline_ablation_config(config_path)

    assert [item.scenario_id for item in loaded.scenarios] == ["family-s7", "family-s8"]
