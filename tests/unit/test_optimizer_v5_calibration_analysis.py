"""Tests for conservative V5 calibration label authorization."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from trustaero.experiments.optimizer_v5_calibration_analysis import (
    _authorized_oracle_set,
)
from trustaero.experiments.real_data_candidate_pilot import (
    load_candidate_pilot_config,
)


def _claim(candidate: str, conclusion: str, authorized: bool = True) -> dict[str, object]:
    return {
        "candidate_id": candidate,
        "conclusion": conclusion,
        "claim_authorized": authorized,
    }


def test_fused_wins_only_when_every_candidate_is_conclusively_slower() -> None:
    claims = [
        _claim("materialized-a", "MATERIALLY_SLOWER"),
        _claim("materialized-b", "MATERIALLY_SLOWER"),
    ]
    assert _authorized_oracle_set(claims) == ["fused"]


def test_one_faster_candidate_can_receive_the_label() -> None:
    claims = [
        _claim("materialized-a", "MATERIALLY_FASTER"),
        _claim("materialized-b", "MATERIALLY_SLOWER"),
    ]
    assert _authorized_oracle_set(claims) == ["materialized-a"]


def test_inconclusive_or_two_faster_candidates_remain_unlabelled() -> None:
    assert (
        _authorized_oracle_set(
            [
                _claim("materialized-a", "INCONCLUSIVE", False),
                _claim("materialized-b", "MATERIALLY_SLOWER"),
            ]
        )
        is None
    )
    assert (
        _authorized_oracle_set(
            [
                _claim("materialized-a", "MATERIALLY_FASTER"),
                _claim("materialized-b", "MATERIALLY_FASTER"),
            ]
        )
        is None
    )


def test_practical_equivalence_keeps_a_set_valued_label() -> None:
    claims = [
        _claim("materialized-a", "PRACTICALLY_EQUIVALENT"),
        _claim("materialized-b", "MATERIALLY_SLOWER"),
    ]
    assert _authorized_oracle_set(claims) == ["fused", "materialized-a"]


def test_measurement_config_loader_materializes_optional_null_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        """{
          "results_dir": "results/test",
          "workloads": ["bts"],
          "sample_rows": [100000],
          "scientific_label": "real_data_multi_candidate_pilot_not_paper_evidence"
        }""",
        encoding="utf-8",
    )
    normalized = asdict(load_candidate_pilot_config(path))
    assert normalized["query_family_protocol_sha256"] is None
    assert normalized["semantic_smoke_sha256"] is None

    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        """{
          "results_dir": "results/test",
          "workloads": ["bts"],
          "sample_rows": [100000],
          "scientific_label": "real_data_multi_candidate_pilot_not_paper_evidence",
          "query_family_protocol_sha256": null,
          "semantic_smoke_sha256": null
        }""",
        encoding="utf-8",
    )
    assert load_candidate_pilot_config(path) == load_candidate_pilot_config(explicit)
