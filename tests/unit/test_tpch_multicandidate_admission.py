"""Contracts for the Q3/Q10 balanced multi-candidate admission."""

from __future__ import annotations

from pathlib import Path

import pytest

from trustaero.experiments.tpch_multicandidate_admission import (
    TpchAdmissionMeasurement,
    _analyze_query,
    load_tpch_multicandidate_admission_config,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/tpch_multicandidate_admission_v1.json"


def test_frozen_admission_shape_and_balance() -> None:
    config = load_tpch_multicandidate_admission_config(CONFIG)
    assert config.scale_factor == 1
    assert dict(config.query_targets)["q03"][-1] == "q03-aggregate"
    assert config.measured_rounds_per_permutation == 2
    assert 24 * config.measured_rounds_per_permutation == 48


def test_analysis_requires_distinct_material_winner() -> None:
    config = load_tpch_multicandidate_admission_config(CONFIG)
    candidates = ("fused", "a", "b", "c")
    rows = []
    for block in range(1, 49):
        for position, candidate in enumerate(candidates, start=1):
            latency = {"fused": 12.0, "a": 10.0, "b": 14.0, "c": 16.0}[candidate]
            rows.append(
                TpchAdmissionMeasurement(
                    query_id="q03",
                    block_index=block,
                    permutation_index=(block - 1) % 24,
                    repetition=(block - 1) // 24,
                    position=position,
                    candidate_id=candidate,
                    latency_ms=latency,
                    row_count=10,
                    result_digest="sha256:stable",
                )
            )
    analysis = _analyze_query("q03", rows, config)
    assert analysis["singleton_winner"] == "a"
    assert analysis["stability_pass"] is True


def test_analysis_retains_near_tie_as_inconclusive() -> None:
    config = load_tpch_multicandidate_admission_config(CONFIG)
    candidates = ("fused", "a", "b", "c")
    rows = []
    for block in range(1, 49):
        for position, candidate in enumerate(candidates, start=1):
            latency = {"fused": 10.0, "a": 10.1, "b": 12.0, "c": 13.0}[candidate]
            rows.append(
                TpchAdmissionMeasurement(
                    query_id="q10",
                    block_index=block,
                    permutation_index=(block - 1) % 24,
                    repetition=(block - 1) // 24,
                    position=position,
                    candidate_id=candidate,
                    latency_ms=latency,
                    row_count=20,
                    result_digest="sha256:stable",
                )
            )
    analysis = _analyze_query("q10", rows, config)
    assert analysis["singleton_winner"] is None


def test_config_rejects_weaker_measurement_count(tmp_path: Path) -> None:
    payload = CONFIG.read_text(encoding="utf-8").replace(
        '"measured_rounds_per_permutation": 2',
        '"measured_rounds_per_permutation": 1',
    )
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="two rounds"):
        load_tpch_multicandidate_admission_config(path)
