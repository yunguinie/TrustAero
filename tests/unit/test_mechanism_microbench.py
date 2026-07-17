"""Tests for checkpointed DuckDB mechanism microbenchmarks."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

import trustaero.experiments.mechanism_microbench as microbench
from trustaero.experiments.mechanism_microbench import (
    MechanismMicrobenchConfig,
    load_mechanism_microbench_config,
    mechanism_microbench_units,
    run_mechanism_microbench,
)


def _config(results_dir: Path) -> MechanismMicrobenchConfig:
    return MechanismMicrobenchConfig(
        results_dir=str(results_dir),
        row_counts=(128,),
        identifier_widths=(32,),
        match_rates=(0.5,),
        seeds=(1,),
        warmup_runs=0,
        measured_runs=1,
        profile_runs=1,
        duckdb_threads=1,
        duckdb_memory_limit_mb=512,
        require_clean_git=False,
    )


def test_units_do_not_cross_irrelevant_match_rate_dimension(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path / "units"),
        match_rates=(0.1, 0.5, 1.0),
        seeds=(1, 2),
    )

    units = mechanism_microbench_units(config)

    assert len([unit for unit in units if unit.benchmark == "hash"]) == 2
    assert len([unit for unit in units if unit.benchmark == "materialization"]) == 2
    assert len([unit for unit in units if unit.benchmark == "join_payload"]) == 6


def test_microbench_writes_valid_complete_artifacts_and_resumes(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    config = _config(tmp_path / "run")

    output = run_mechanism_microbench(config)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
    with (output / "raw_measurements.csv").open(newline="", encoding="utf-8") as handle:
        measurements = list(csv.DictReader(handle))
    with (output / "paired_costs.csv").open(newline="", encoding="utf-8") as handle:
        paired = list(csv.DictReader(handle))
    with (output / "operator_summary.csv").open(newline="", encoding="utf-8") as handle:
        operators = list(csv.DictReader(handle))

    assert summary["status"] == "complete"
    assert summary["all_validations_passed"] is True
    assert summary["unit_count"] == 3
    assert summary["measurement_count"] == 6
    assert checkpoint["status"] == "complete"
    assert len(measurements) == 6
    assert any(row["operator_name"] == "HASH_JOIN" for row in operators)
    assert {row["derived_component"] for row in paired} == {
        "hash_incremental",
        "join_incremental",
        "materialization_roundtrip",
    }
    assert (output / "progress.json").exists()

    resumed = run_mechanism_microbench(config, resume_run_id=output.name)
    assert resumed == output


def test_resume_rejects_commit_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("duckdb")
    config = _config(tmp_path / "resume")
    output = run_mechanism_microbench(config)
    original = json.loads((output / "environment.json").read_text(encoding="utf-8"))[
        "commit_hash"
    ]
    monkeypatch.setattr(microbench, "_git_commit", lambda _root: original + "-changed")

    with pytest.raises(ValueError, match="Git commit changed"):
        run_mechanism_microbench(config, resume_run_id=output.name)


def test_loader_and_validation_reject_duplicate_dimensions(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "results_dir": str(tmp_path / "loaded"),
                "row_counts": [100],
                "identifier_widths": [64],
                "match_rates": [0.25],
                "seeds": [3],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_mechanism_microbench_config(config_path)

    assert loaded.row_counts == (100,)
    with pytest.raises(ValueError, match="identifier_widths cannot contain duplicates"):
        replace(loaded, identifier_widths=(64, 64))


def test_atomic_json_retries_transient_windows_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = microbench.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient reader lock")
        real_replace(source, destination)

    monkeypatch.setattr(microbench.os, "replace", flaky_replace)
    path = tmp_path / "progress.json"

    microbench._write_json_atomic(path, {"complete": True})

    assert attempts == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"complete": True}
