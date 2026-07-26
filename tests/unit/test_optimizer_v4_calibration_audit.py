"""Tests for V4 expanded calibration paired auditing."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.experiments.optimizer_v4_calibration_audit import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    _regret_percent,
    audit_optimizer_v4_calibration,
)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_paired_regret_uses_ratio_direction() -> None:
    assert _regret_percent(1.5, EARLY_CANDIDATE) == 50.0
    assert _regret_percent(1.5, LATE_CANDIDATE) == 0.0
    assert _regret_percent(0.5, EARLY_CANDIDATE) == 0.0
    assert _regret_percent(0.5, LATE_CANDIDATE) == 100.0


def test_v4_calibration_audit_preserves_stable_reversal(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(run / "summary.json", {"status": "PASS_STRUCTURAL_GATE"})
    _write(
        run / "config.json",
        {"tie_threshold_fraction": 0.03, "measured_blocks": 4},
    )
    for family_id, early_latency, late_latency in (
        ("early-family", 50.0, 100.0),
        ("late-family", 150.0, 100.0),
    ):
        timings = []
        for block in range(4):
            order = (EARLY_CANDIDATE, LATE_CANDIDATE)
            if block % 2:
                order = tuple(reversed(order))
            for position, candidate in enumerate(order):
                timings.append(
                    {
                        "block_index": block,
                        "candidate_id": candidate,
                        "order_position": position,
                        "latency_ms": early_latency
                        if candidate == EARLY_CANDIDATE
                        else late_latency,
                    }
                )
        _write(
            run / "families" / f"{family_id}.json",
            {
                "family_id": family_id,
                "scenario_group": "window",
                "identifier_width_bytes": 192,
                "target_match_rate": 0.5,
                "achieved_join_match_rate": 0.5,
                "join_input_rows": 100,
                "timings": timings,
            },
        )

    audit = audit_optimizer_v4_calibration(run, bootstrap_repetitions=100)

    assert audit["stable_family_count"] == 2
    assert audit["stable_early_preferred_count"] == 1
    assert audit["stable_late_preferred_count"] == 1
    assert audit["measurement_count"] == 16
    assert audit["model_fitted"] is False
