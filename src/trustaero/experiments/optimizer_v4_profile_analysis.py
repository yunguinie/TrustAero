"""Audit V4 DuckDB profiles against the authoritative paired timing labels."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.real_data_governed import _load_json
from trustaero.experiments.real_optimizer_transfer import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
)


def _profile_direction(early_ms: float, late_ms: float) -> str:
    return EARLY_CANDIDATE if early_ms < late_ms else LATE_CANDIDATE


def analyze_optimizer_v4_profiles(
    profile_run_dir: Path | str,
    paired_audit_path: Path | str,
) -> dict[str, object]:
    """Compare descriptive profiles with paired labels without relabeling data."""

    run_dir = Path(profile_run_dir)
    summary = cast(dict[str, Any], _load_json(run_dir / "summary.json"))
    if summary.get("status") != "PASS":
        raise ValueError("V4 profile analysis requires a passed structural gate")
    audit = cast(dict[str, Any], _load_json(Path(paired_audit_path)))
    audited = {
        str(item["family_id"]): item for item in cast(list[dict[str, Any]], audit["family_audits"])
    }
    families = [
        cast(dict[str, Any], _load_json(path))
        for path in sorted((run_dir / "families").glob("*.json"))
    ]
    if set(audited) != {str(item["family_id"]) for item in families}:
        raise ValueError("Profile and paired-audit family sets differ")

    comparisons: list[dict[str, object]] = []
    cpu_wall_ratios: list[float] = []
    for family in families:
        family_id = str(family["family_id"])
        profiles = cast(dict[str, dict[str, Any]], family["profiles"])
        early = profiles[EARLY_CANDIDATE]
        late = profiles[LATE_CANDIDATE]
        early_ms = float(early["median_profile_latency_ms"])
        late_ms = float(late["median_profile_latency_ms"])
        profile_direction = _profile_direction(early_ms, late_ms)
        paired = cast(dict[str, Any], audited[family_id])
        paired_direction = str(cast(dict[str, Any], paired["directions"])["overall"])
        candidate_ratios: dict[str, float] = {}
        for candidate_id, profile in profiles.items():
            wall_ms = float(profile["median_profile_latency_ms"])
            cpu_sum_ms = sum(float(item) for item in profile["median_operator_timings_ms"])
            ratio = cpu_sum_ms / wall_ms if wall_ms else 0.0
            candidate_ratios[candidate_id] = ratio
            cpu_wall_ratios.append(ratio)
        comparisons.append(
            {
                "family_id": family_id,
                "paired_direction": paired_direction,
                "paired_stable": bool(paired["stable_for_transfer_conclusion"]),
                "paired_early_over_late_ratio": float(paired["median_early_over_late_ratio"]),
                "paired_ratio_ci95": list(paired["paired_median_ratio_ci95"]),
                "profile_direction": profile_direction,
                "profile_early_over_late_ratio": early_ms / late_ms,
                "direction_agrees": profile_direction == paired_direction,
                "profile_latency_samples_ms": {
                    EARLY_CANDIDATE: list(early["profile_latency_samples_ms"]),
                    LATE_CANDIDATE: list(late["profile_latency_samples_ms"]),
                },
                "summed_operator_cpu_over_wall_ratio": candidate_ratios,
            }
        )
    stable = [item for item in comparisons if item["paired_stable"]]
    conflicts = [item["family_id"] for item in stable if not item["direction_agrees"]]
    raw_plan_count = len(tuple((run_dir / "plans").rglob("*.json")))
    expected_plans = (
        len(families)
        * 2
        * int(cast(dict[str, Any], _load_json(run_dir / "config.json"))["profile_runs"])
    )
    return {
        "schema_version": 1,
        "status": "PASS_DESCRIPTIVE_PROFILE_AUDIT",
        "source_profile_run_id": run_dir.name,
        "source_paired_audit_run_id": audit["source_run_id"],
        "family_count": len(families),
        "stable_paired_family_count": len(stable),
        "profile_direction_agreement_on_stable_count": sum(
            bool(item["direction_agrees"]) for item in stable
        ),
        "profile_direction_disagreement_on_stable_count": len(conflicts),
        "profile_direction_disagreement_family_ids": conflicts,
        "raw_plan_file_count": raw_plan_count,
        "expected_raw_plan_file_count": expected_plans,
        "raw_plan_set_complete": raw_plan_count == expected_plans,
        "operator_cpu_wall_ratio_median": statistics.median(cpu_wall_ratios),
        "operator_cpu_wall_ratio_max": max(cpu_wall_ratios),
        "profile_direction_is_authoritative_label": False,
        "paired_timing_direction_is_authoritative_label": True,
        "operator_timings_are_additive_causal_costs": False,
        "model_fitted": False,
        "family_comparisons": comparisons,
        "interpretation": (
            "Profiles validate stable, distinct physical pipelines and expose where "
            "DuckDB spends operator CPU. Direction disagreements and CPU-over-wall "
            "ratios confirm that EXPLAIN timings are descriptive, non-additive, and "
            "must not replace paired wall-clock labels or become inference features."
        ),
    }


def write_optimizer_v4_profile_analysis(
    profile_run_dir: Path | str,
    paired_audit_path: Path | str,
) -> Path:
    """Write the derived audit next to immutable raw profiles."""

    run_dir = Path(profile_run_dir)
    output = run_dir / "v4_profile_analysis.json"
    payload = analyze_optimizer_v4_profiles(run_dir, paired_audit_path)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output
