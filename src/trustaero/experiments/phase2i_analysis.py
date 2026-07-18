"""Deterministic analysis for complete early/late Mask fragment pilots.

The analysis uses the already frozen 3% practical-tie band.  It aggregates
complete seed families and reports reversals without fitting an optimizer or
changing any measured timing.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, cast


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing Phase 2I artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _classify(
    early_ms: float,
    late_ms: float,
    tie_threshold_fraction: float,
) -> str:
    """Return a winner only when it is more than the practical tie band faster."""

    if early_ms < late_ms * (1.0 - tie_threshold_fraction):
        return "early"
    if late_ms < early_ms * (1.0 - tie_threshold_fraction):
        return "late"
    return "tie"


def _unit_rows(
    component_rows: list[dict[str, str]],
    tie_threshold_fraction: float,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int, float, int], dict[str, dict[str, str]]] = {}
    for row in component_rows:
        if row["benchmark"] != "mask_fragment":
            continue
        key = (
            int(row["row_count"]),
            int(row["identifier_width"]),
            float(row["match_rate"]),
            int(row["seed"]),
        )
        components = grouped.setdefault(key, {})
        component = row["component"]
        if component in components:
            raise ValueError(f"Duplicate Phase 2I component for {key}: {component}")
        components[component] = row
    output: list[dict[str, Any]] = []
    expected = {"early_mask_fragment", "late_mask_fragment"}
    for (rows, width, match_rate, seed), components in sorted(grouped.items()):
        if set(components) != expected:
            raise ValueError(f"Phase 2I unit lacks one complete candidate: {components}")
        early = float(components["early_mask_fragment"]["median_latency_ms"])
        late = float(components["late_mask_fragment"]["median_latency_ms"])
        if early <= 0.0 or late <= 0.0:
            raise ValueError("Phase 2I median latency must be positive")
        winner = _classify(early, late, tie_threshold_fraction)
        output.append(
            {
                "family_id": f"n{rows}-w{width}-m{round(match_rate * 1000):04d}",
                "row_count": rows,
                "identifier_width": width,
                "match_rate": match_rate,
                "seed": seed,
                "early_median_latency_ms": early,
                "late_median_latency_ms": late,
                "early_minus_late_percent": (early / late - 1.0) * 100.0,
                "classification": winner,
                "winner_speedup_ratio": max(early, late) / min(early, late),
                "tie_threshold_fraction": tie_threshold_fraction,
            }
        )
    if not output:
        raise ValueError("No Phase 2I Mask fragment units were found")
    return output


def _family_rows(
    unit_rows: list[dict[str, Any]],
    required_seed_agreement_fraction: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in unit_rows:
        grouped.setdefault(str(row["family_id"]), []).append(row)
    seed_counts = {len(rows) for rows in grouped.values()}
    if len(seed_counts) != 1:
        raise ValueError("Phase 2I scenario families have inconsistent seed counts")
    output: list[dict[str, Any]] = []
    for family_id, rows in sorted(grouped.items()):
        classes = [str(row["classification"]) for row in rows]
        required_count = math.ceil(required_seed_agreement_fraction * len(rows))
        if classes.count("early") >= required_count:
            classification = "stable_early"
        elif classes.count("late") >= required_count:
            classification = "stable_late"
        elif classes.count("tie") >= required_count:
            classification = "stable_tie"
        else:
            classification = "mixed"
        early_values = [float(row["early_median_latency_ms"]) for row in rows]
        late_values = [float(row["late_median_latency_ms"]) for row in rows]
        median_early = statistics.median(early_values)
        median_late = statistics.median(late_values)
        paired_relative_differences = [
            (early / late - 1.0) * 100.0
            for early, late in zip(early_values, late_values, strict=True)
        ]
        paired_speedups = [
            max(early, late) / min(early, late)
            for early, late in zip(early_values, late_values, strict=True)
        ]
        output.append(
            {
                "family_id": family_id,
                "row_count": rows[0]["row_count"],
                "identifier_width": rows[0]["identifier_width"],
                "match_rate": rows[0]["match_rate"],
                "seed_count": len(rows),
                "required_seed_agreement_count": required_count,
                "early_count": classes.count("early"),
                "late_count": classes.count("late"),
                "tie_count": classes.count("tie"),
                "family_classification": classification,
                "median_early_latency_ms": median_early,
                "median_late_latency_ms": median_late,
                # Preserve the paired-seed design: compute each seed's
                # relative difference first, then aggregate those ratios.
                "median_early_minus_late_percent": statistics.median(paired_relative_differences),
                "median_winner_speedup_ratio": statistics.median(paired_speedups),
            }
        )
    return output


def _report(summary: dict[str, Any], families: list[dict[str, Any]]) -> str:
    lines = [
        "# Complete Mask-fragment family analysis",
        "",
        "This is development evidence, not an independent Phase 2G result.",
        "",
        f"- Units: {summary['unit_count']}",
        f"- Scenario families: {summary['family_count']}",
        f"- Stable early families: {summary['stable_early_family_count']}",
        f"- Stable late families: {summary['stable_late_family_count']}",
        f"- Stable tie families: {summary['stable_tie_family_count']}",
        f"- Mixed families: {summary['mixed_family_count']}",
        f"- Stable reversal observed: {summary['stable_reversal_observed']}",
        f"- Required seed agreement: {summary['required_seed_agreement_fraction']:.0%}",
        f"- Optimizer-design gate passed: {summary['optimizer_design_gate_passes']}",
        "",
        "| Rows | Width | Match | Early/Tie/Late seeds | Family | Early ms | Late ms |",
        "|---:|---:|---:|---:|---|---:|---:|",
    ]
    for row in families:
        lines.append(
            "| {rows} | {width} | {match:.0%} | {early}/{tie}/{late} | {family} | "
            "{early_ms:.2f} | {late_ms:.2f} |".format(
                rows=row["row_count"],
                width=row["identifier_width"],
                match=row["match_rate"],
                early=row["early_count"],
                tie=row["tie_count"],
                late=row["late_count"],
                family=row["family_classification"],
                early_ms=row["median_early_latency_ms"],
                late_ms=row["median_late_latency_ms"],
            )
        )
    lines.extend(
        [
            "",
            "Passing this gate permits pipeline-aware model design only. It does not ",
            "authorize Phase 2G or a held-out generalization claim.",
        ]
    )
    return "\n".join(lines) + "\n"


def _stable_early_region_has_adjacent_families(
    families: list[dict[str, Any]],
) -> bool:
    """Detect a neighboring pair on the observed rows/width/match grid."""

    early_families = [row for row in families if row["family_classification"] == "stable_early"]
    row_axis = sorted({int(row["row_count"]) for row in families})
    width_axis = sorted({int(row["identifier_width"]) for row in families})
    match_axis = sorted({float(row["match_rate"]) for row in families})

    def adjacent(left: dict[str, Any], right: dict[str, Any]) -> bool:
        indices_left = (
            row_axis.index(int(left["row_count"])),
            width_axis.index(int(left["identifier_width"])),
            match_axis.index(float(left["match_rate"])),
        )
        indices_right = (
            row_axis.index(int(right["row_count"])),
            width_axis.index(int(right["identifier_width"])),
            match_axis.index(float(right["match_rate"])),
        )
        differences = [abs(a - b) for a, b in zip(indices_left, indices_right, strict=True)]
        return sorted(differences) == [0, 0, 1]

    return any(
        adjacent(left, right)
        for index, left in enumerate(early_families)
        for right in early_families[index + 1 :]
    )


def analyze_phase2i_fragment_run(
    run_dir_value: str | Path,
    output_dir_value: str | Path,
    *,
    tie_threshold_fraction: float = 0.03,
    required_seed_agreement_fraction: float = 1.0,
    evaluation_label: str = "phase2i_fragment_pilot_development_analysis",
) -> Path:
    """Validate a complete pilot and emit deterministic unit/family summaries."""

    if not 0.0 <= tie_threshold_fraction < 1.0:
        raise ValueError("tie_threshold_fraction must be in [0, 1)")
    if not 0.5 < required_seed_agreement_fraction <= 1.0:
        raise ValueError("required_seed_agreement_fraction must be in (0.5, 1]")
    if not evaluation_label:
        raise ValueError("evaluation_label cannot be empty")
    run_dir = Path(run_dir_value).resolve()
    output_dir = Path(output_dir_value).resolve()
    summary = _read_object(run_dir / "summary.json")
    if summary.get("status") != "complete" or summary.get("all_validations_passed") is not True:
        raise ValueError("Phase 2I source run is incomplete or failed validation")
    unit_count = int(summary["unit_count"])
    if (
        int(summary.get("result_equivalent_fragment_count", -1)) != unit_count
        or int(summary.get("distinct_physical_plan_fragment_count", -1)) != unit_count
    ):
        raise ValueError("Phase 2I source run lacks complete equivalence/plan evidence")
    units = _unit_rows(_read_csv(run_dir / "component_summary.csv"), tie_threshold_fraction)
    if len(units) != unit_count:
        raise ValueError("Phase 2I component summary does not cover every unit")
    families = _family_rows(units, required_seed_agreement_fraction)
    class_counts = {
        name: sum(row["family_classification"] == name for row in families)
        for name in ("stable_early", "stable_late", "stable_tie", "mixed")
    }
    unit_class_counts = {
        name: sum(row["classification"] == name for row in units)
        for name in ("early", "late", "tie")
    }
    environment = _read_object(run_dir / "environment.json")
    early_families = [row for row in families if row["family_classification"] == "stable_early"]
    early_region_adjacent = _stable_early_region_has_adjacent_families(families)
    optimizer_design_checks = {
        "at_least_two_stable_early_families": len(early_families) >= 2,
        "at_least_one_stable_late_family": class_counts["stable_late"] >= 1,
        "stable_early_region_has_adjacent_families": early_region_adjacent,
        "no_spilled_units": int(summary.get("spilled_unit_count", 0)) == 0,
    }
    optimizer_design_gate_passes = all(optimizer_design_checks.values())
    analysis_summary: dict[str, Any] = {
        "evaluation_label": evaluation_label,
        "source_run_id": str(summary["run_id"]),
        "source_commit_hash": str(environment.get("commit_hash", "unknown")),
        "tie_threshold_fraction": tie_threshold_fraction,
        "required_seed_agreement_fraction": required_seed_agreement_fraction,
        "unit_count": len(units),
        "family_count": len(families),
        "unit_classification_counts": unit_class_counts,
        "stable_early_family_count": class_counts["stable_early"],
        "stable_late_family_count": class_counts["stable_late"],
        "stable_tie_family_count": class_counts["stable_tie"],
        "mixed_family_count": class_counts["mixed"],
        "stable_reversal_observed": (
            class_counts["stable_early"] > 0 and class_counts["stable_late"] > 0
        ),
        "stable_early_family_ids": [
            row["family_id"] for row in families if row["family_classification"] == "stable_early"
        ],
        "spilled_unit_count": int(summary.get("spilled_unit_count", 0)),
        "phase2g_authorized": False,
        "optimizer_design_gate": optimizer_design_checks,
        "optimizer_design_gate_passes": optimizer_design_gate_passes,
        "optimizer_training_recommendation": (
            "eligible_for_pipeline_model_design"
            if optimizer_design_gate_passes
            else "defer_optimizer_training"
        ),
        "scientific_boundary": (
            "The fixed 3% tie band classifies complete seed families. No optimizer "
            "is fitted, and the development pilot is not held-out Phase 2G evidence."
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "unit_summary.csv", units)
    _write_csv(output_dir / "family_summary.csv", families)
    (output_dir / "analysis_summary.json").write_text(
        json.dumps(analysis_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(_report(analysis_summary, families), encoding="utf-8")
    return output_dir
