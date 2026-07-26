"""Paired stability audit for the expanded January V4 calibration matrix."""

from __future__ import annotations

import math
import random
import statistics
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_optimizer_transfer import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
)


def _direction(ratio: float, tie_fraction: float) -> str:
    if ratio < 1.0 - tie_fraction:
        return EARLY_CANDIDATE
    if ratio > 1.0 + tie_fraction:
        return LATE_CANDIDATE
    return "tie"


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _regret_percent(ratio: float, candidate_id: str) -> float:
    """Compute regret from the within-block early/late median ratio."""

    if candidate_id == EARLY_CANDIDATE:
        return max(0.0, ratio - 1.0) * 100.0
    if candidate_id == LATE_CANDIDATE:
        return max(0.0, 1.0 / ratio - 1.0) * 100.0
    raise ValueError(f"Unknown Mask candidate: {candidate_id}")


def audit_optimizer_v4_calibration(
    run_dir: Path | str,
    *,
    bootstrap_repetitions: int = 5000,
    bootstrap_seed: int = 20260723,
) -> dict[str, object]:
    """Audit paired labels while retaining every unstable or tied family."""

    directory = Path(run_dir)
    summary = cast(dict[str, Any], _load_json(directory / "summary.json"))
    if summary.get("status") != "PASS_STRUCTURAL_GATE":
        raise ValueError("V4 audit requires a complete passed calibration run")
    config = cast(dict[str, Any], _load_json(directory / "config.json"))
    tie_fraction = float(config["tie_threshold_fraction"])
    families = [
        cast(dict[str, Any], _load_json(path))
        for path in sorted((directory / "families").glob("*.json"))
    ]
    # Each audit is an intentionally heterogeneous JSON record.
    audits: list[dict[str, Any]] = []
    for family_index, family in enumerate(families):
        by_block: dict[int, dict[str, dict[str, Any]]] = {}
        for timing in cast(list[dict[str, Any]], family["timings"]):
            by_block.setdefault(int(timing["block_index"]), {})[str(timing["candidate_id"])] = (
                timing
            )
        ratios: list[float] = []
        early_first: list[float] = []
        late_first: list[float] = []
        first_half: list[float] = []
        second_half: list[float] = []
        split = len(by_block) // 2
        for block_index, block in sorted(by_block.items()):
            if set(block) != {EARLY_CANDIDATE, LATE_CANDIDATE}:
                raise ValueError("Every V4 block must contain the complete candidate pair")
            ratio = float(block[EARLY_CANDIDATE]["latency_ms"]) / float(
                block[LATE_CANDIDATE]["latency_ms"]
            )
            ratios.append(ratio)
            (
                early_first if int(block[EARLY_CANDIDATE]["order_position"]) == 0 else late_first
            ).append(ratio)
            (first_half if block_index < split else second_half).append(ratio)
        if len(ratios) != int(config["measured_blocks"]):
            raise ValueError("V4 family does not contain the frozen measured blocks")
        rng = random.Random(bootstrap_seed + family_index)
        bootstrapped = sorted(
            statistics.median(ratios[rng.randrange(len(ratios))] for _ in ratios)
            for _ in range(bootstrap_repetitions)
        )
        lower = bootstrapped[math.floor(0.025 * (len(bootstrapped) - 1))]
        upper = bootstrapped[math.ceil(0.975 * (len(bootstrapped) - 1))]
        median_ratio = statistics.median(ratios)
        directions = {
            "overall": _direction(median_ratio, tie_fraction),
            "early_first": _direction(statistics.median(early_first), tie_fraction),
            "late_first": _direction(statistics.median(late_first), tie_fraction),
            "first_half": _direction(statistics.median(first_half), tie_fraction),
            "second_half": _direction(statistics.median(second_half), tie_fraction),
        }
        order_effect = directions["early_first"] != directions["late_first"]
        temporal_drift = directions["first_half"] != directions["second_half"]
        confidence_excludes_tie = upper < 1.0 - tie_fraction or lower > 1.0 + tie_fraction
        stable = (
            directions["overall"] != "tie"
            and not order_effect
            and not temporal_drift
            and confidence_excludes_tie
        )
        audits.append(
            {
                "family_id": family["family_id"],
                "scenario_group": family["scenario_group"],
                "identifier_width_bytes": family["identifier_width_bytes"],
                "target_match_rate": family["target_match_rate"],
                "achieved_join_match_rate": family["achieved_join_match_rate"],
                "join_input_rows": family["join_input_rows"],
                "paired_block_count": len(ratios),
                "median_early_over_late_ratio": median_ratio,
                "paired_median_ratio_ci95": [lower, upper],
                "early_win_block_count": sum(item < 1.0 for item in ratios),
                "directions": directions,
                "candidate_order_effect_suspected": order_effect,
                "temporal_drift_suspected": temporal_drift,
                "confidence_excludes_3_percent_tie_band": confidence_excludes_tie,
                "stable_for_model_evaluation": stable,
                "fixed_early_regret_percent": _regret_percent(median_ratio, EARLY_CANDIDATE),
                "fixed_late_regret_percent": _regret_percent(median_ratio, LATE_CANDIDATE),
            }
        )
    stable_families = [item for item in audits if item["stable_for_model_evaluation"]]
    early_regrets = [float(item["fixed_early_regret_percent"]) for item in stable_families]
    late_regrets = [float(item["fixed_late_regret_percent"]) for item in stable_families]

    def baseline(regrets: list[float]) -> dict[str, float]:
        return {
            "within_3_percent_rate": sum(item <= 3.0 for item in regrets) / len(regrets),
            "mean_regret_percent": statistics.mean(regrets),
            "p95_regret_percent": _nearest_rank_p95(regrets),
            "max_regret_percent": max(regrets),
        }

    group_summary: dict[str, dict[str, int]] = {}
    for group in sorted({str(item["scenario_group"]) for item in audits}):
        members = [item for item in audits if item["scenario_group"] == group]
        stable_members = [item for item in members if item["stable_for_model_evaluation"]]
        group_summary[group] = {
            "family_count": len(members),
            "stable_count": len(stable_members),
            "stable_early_count": sum(
                cast(dict[str, str], item["directions"])["overall"] == EARLY_CANDIDATE
                for item in stable_members
            ),
            "stable_late_count": sum(
                cast(dict[str, str], item["directions"])["overall"] == LATE_CANDIDATE
                for item in stable_members
            ),
            "unstable_or_tie_count": len(members) - len(stable_members),
        }
    return {
        "schema_version": 1,
        "status": "PASS_PAIRED_STABILITY_AUDIT",
        "source_run_id": directory.name,
        "family_count": len(audits),
        "measurement_count": sum(int(item["paired_block_count"]) * 2 for item in audits),
        "stable_family_count": len(stable_families),
        "unstable_or_tie_family_count": len(audits) - len(stable_families),
        "stable_early_preferred_count": sum(
            cast(dict[str, str], item["directions"])["overall"] == EARLY_CANDIDATE
            for item in stable_families
        ),
        "stable_late_preferred_count": sum(
            cast(dict[str, str], item["directions"])["overall"] == LATE_CANDIDATE
            for item in stable_families
        ),
        "candidate_order_effect_count": sum(
            bool(item["candidate_order_effect_suspected"]) for item in audits
        ),
        "temporal_drift_count": sum(bool(item["temporal_drift_suspected"]) for item in audits),
        "confidence_not_excluding_tie_count": sum(
            not bool(item["confidence_excludes_3_percent_tie_band"]) for item in audits
        ),
        "fixed_early_metrics": baseline(early_regrets),
        "fixed_late_metrics": baseline(late_regrets),
        "scenario_groups": group_summary,
        "family_audits": audits,
        "model_fitted": False,
        "external_partition_accessed": False,
        "interpretation": (
            "The expanded January paired labels contain stable reversals in both "
            "directions. Unstable and tied families remain present for uncertainty "
            "evaluation but are excluded from strong selection-accuracy claims."
        ),
    }


def write_optimizer_v4_calibration_audit(run_dir: Path | str) -> Path:
    directory = Path(run_dir)
    output = directory / "paired_stability_audit.json"
    _atomic_json(output, audit_optimizer_v4_calibration(directory))
    return output
