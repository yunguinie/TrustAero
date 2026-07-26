"""Tests for the fail-closed Optimizer V3 readiness boundary."""

from __future__ import annotations

import csv
from pathlib import Path

from trustaero.experiments.optimizer_v3_readiness import analyze_position_effects

FIELDS = (
    "component",
    "identifier_width",
    "latency_ms",
    "match_rate",
    "order_position",
    "row_count",
    "run_id",
    "unit_id",
)


def _write_measurements(path: Path, *, biased: bool = False) -> None:
    rows: list[dict[str, object]] = []
    for family in range(4):
        for seed in range(5):
            unit_id = f"unit-{family}-{seed}"
            for component in ("early_mask_fragment", "late_mask_fragment"):
                for repeat in range(5):
                    first = 100.0 + family + seed * 0.1 + repeat * 0.01
                    second = first * (1.3 if biased else 1.01)
                    rows.extend(
                        (
                            {
                                "component": component,
                                "identifier_width": 128 * (family + 1),
                                "latency_ms": first,
                                "match_rate": 0.9,
                                "order_position": 0,
                                "row_count": 100_000,
                                "run_id": "fixture-run",
                                "unit_id": unit_id,
                            },
                            {
                                "component": component,
                                "identifier_width": 128 * (family + 1),
                                "latency_ms": second,
                                "match_rate": 0.9,
                                "order_position": 1,
                                "row_count": 100_000,
                                "run_id": "fixture-run",
                                "unit_id": unit_id,
                            },
                        )
                    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_position_effect_accepts_balanced_negligible_bias(tmp_path: Path) -> None:
    path = tmp_path / "raw_measurements.csv"
    _write_measurements(path)

    effects, balanced, evidence = analyze_position_effects(
        path,
        tolerance_fraction=0.1,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=7,
    )

    assert balanced is True
    assert evidence["balance_failures"] == []
    assert len(effects) == 2
    assert all(effect.passed for effect in effects)


def test_position_effect_rejects_material_systematic_bias(tmp_path: Path) -> None:
    path = tmp_path / "raw_measurements.csv"
    _write_measurements(path, biased=True)

    effects, balanced, _ = analyze_position_effects(
        path,
        tolerance_fraction=0.1,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=7,
    )

    assert balanced is True
    assert all(effect.passed is False for effect in effects)


def test_position_effect_rejects_missing_position(tmp_path: Path) -> None:
    path = tmp_path / "raw_measurements.csv"
    _write_measurements(path)
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    rows = [
        row
        for row in rows
        if not (
            row["unit_id"] == "unit-0-0"
            and row["component"] == "early_mask_fragment"
            and row["order_position"] == "1"
        )
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _, balanced, evidence = analyze_position_effects(
        path,
        tolerance_fraction=0.1,
        confidence_level=0.95,
        bootstrap_repetitions=1000,
        bootstrap_seed=7,
    )

    assert balanced is False
    assert evidence["balance_failures"]
