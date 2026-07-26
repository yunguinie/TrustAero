"""Tests for the one-shot Phase 2G holdout boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trustaero.experiments.mechanism_microbench import (
    load_mechanism_microbench_config,
    mechanism_microbench_units,
)
from trustaero.experiments.phase2g_holdout import (
    Phase2GPreflight,
    create_or_validate_one_shot_manifest,
    stratified_paired_mean_bootstrap_ci,
)

ROOT = Path(__file__).resolve().parents[2]


def test_frozen_holdout_matrix_has_36_new_families_and_180_units() -> None:
    config = load_mechanism_microbench_config(
        ROOT / "experiments/configs/phase2g_optimizer_v3_holdout_v1.json"
    )
    units = mechanism_microbench_units(config)

    assert config.row_counts == (75_000, 125_000, 175_000)
    assert config.identifier_widths == (192, 384, 768, 1536)
    assert config.match_rates == (0.25, 0.7, 0.95)
    assert len(units) == 180
    assert len({(unit.row_count, unit.identifier_width, unit.match_rate) for unit in units}) == 36


def test_paired_mean_bootstrap_is_deterministic_and_preserves_direction() -> None:
    differences = {
        "75000": (0.1, 0.2, 0.3),
        "125000": (0.2, 0.3, 0.4),
        "175000": (0.3, 0.4, 0.5),
    }

    first = stratified_paired_mean_bootstrap_ci(
        differences,
        confidence_level=0.95,
        repetitions=2000,
        seed=17,
    )
    second = stratified_paired_mean_bootstrap_ci(
        differences,
        confidence_level=0.95,
        repetitions=2000,
        seed=17,
    )

    assert first == second
    assert first[0] > 0.0


def _manifest_fixture(tmp_path: Path) -> tuple[Path, Phase2GPreflight]:
    (tmp_path / "model.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "stability.json").write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "protocol_name": "fixture",
                "results_dir": "results/phase2g",
                "primary_model_path": "model.json",
                "stability_models_path": "stability.json",
            }
        ),
        encoding="utf-8",
    )
    preflight = Phase2GPreflight(
        schema_version=1,
        status="PASS",
        source_commit="abc123",
        checks=(),
        may_start_new_run=True,
        may_resume_existing_run=False,
    )
    return config_path, preflight


def test_one_shot_manifest_prevents_second_new_holdout(tmp_path: Path) -> None:
    config_path, preflight = _manifest_fixture(tmp_path)

    manifest = create_or_validate_one_shot_manifest(
        tmp_path,
        config_path,
        preflight,
        resume=False,
    )

    assert manifest.is_file()
    with pytest.raises(ValueError, match="already exists"):
        create_or_validate_one_shot_manifest(
            tmp_path,
            config_path,
            preflight,
            resume=False,
        )


def test_resume_manifest_rejects_source_commit_change(tmp_path: Path) -> None:
    config_path, preflight = _manifest_fixture(tmp_path)
    create_or_validate_one_shot_manifest(
        tmp_path,
        config_path,
        preflight,
        resume=False,
    )
    changed = Phase2GPreflight(
        schema_version=1,
        status="PASS",
        source_commit="different",
        checks=(),
        may_start_new_run=False,
        may_resume_existing_run=True,
    )

    with pytest.raises(ValueError, match="source_commit"):
        create_or_validate_one_shot_manifest(
            tmp_path,
            config_path,
            changed,
            resume=True,
        )
