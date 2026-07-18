"""Policy-aware analysis for the compact Phase 2M ablation matrix."""

from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.pipeline_ablation import PIPELINE_ABLATION_VARIANTS


@dataclass(frozen=True)
class AblationPolicyProfile:
    """Hard feasibility constraints evaluated before runtime comparison."""

    policy_id: str
    allow_raw_join: bool
    allow_raw_materialization: bool


ABLATION_POLICY_PROFILES = (
    AblationPolicyProfile("raw_permissive", True, True),
    AblationPolicyProfile("no_raw_materialization", True, False),
    AblationPolicyProfile("no_raw_join", False, False),
)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing compact ablation artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def variant_is_legal(row: dict[str, str], policy: AblationPolicyProfile) -> bool:
    """Apply exposure policy before looking at a candidate's measured latency."""

    raw_join_rows = int(row["raw_rows_exposed_to_join"])
    raw_materialized_rows = int(row["raw_rows_materialized"])
    if raw_join_rows > 0 and not policy.allow_raw_join:
        return False
    if raw_materialized_rows > 0 and not policy.allow_raw_materialization:
        return False
    return True


def _practical_winner(
    legal_rows: list[dict[str, str]], tie_threshold_fraction: float
) -> tuple[str, str, float, str]:
    """Return a winner only when it beats the runner-up beyond the tie band."""

    if not legal_rows:
        raise ValueError("A policy leaves no legal ablation candidate")
    ordered = sorted(legal_rows, key=lambda row: float(row["median_latency_ms"]))
    fastest = ordered[0]
    fastest_ms = float(fastest["median_latency_ms"])
    if len(ordered) == 1:
        return fastest["variant"], "single_legal_candidate", fastest_ms, fastest["variant"]
    runner_up_ms = float(ordered[1]["median_latency_ms"])
    near = [
        row["variant"]
        for row in ordered
        if float(row["median_latency_ms"]) <= fastest_ms * (1.0 + tie_threshold_fraction)
    ]
    classification = (
        fastest["variant"] if fastest_ms < runner_up_ms * (1.0 - tie_threshold_fraction) else "tie"
    )
    return fastest["variant"], classification, fastest_ms, "|".join(near)


def _unit_policy_rows(
    components: list[dict[str, str]], tie_threshold_fraction: float
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in components:
        grouped.setdefault(row["scenario_id"], []).append(row)
    output: list[dict[str, Any]] = []
    for scenario_id, rows in sorted(grouped.items()):
        if {row["variant"] for row in rows} != set(PIPELINE_ABLATION_VARIANTS):
            raise ValueError(f"Incomplete four-way ablation scenario: {scenario_id}")
        unconstrained = min(float(row["median_latency_ms"]) for row in rows)
        for policy in ABLATION_POLICY_PROFILES:
            legal = [row for row in rows if variant_is_legal(row, policy)]
            winner, classification, winner_ms, near = _practical_winner(
                legal, tie_threshold_fraction
            )
            winner_row = next(row for row in legal if row["variant"] == winner)
            output.append(
                {
                    "scenario_id": scenario_id,
                    "family_id": (
                        f"n{int(rows[0]['row_count'])}-"
                        f"w{int(rows[0]['identifier_width'])}-"
                        f"m{round(float(rows[0]['match_rate']) * 1000):04d}"
                    ),
                    "region_label": rows[0]["region_label"],
                    "row_count": int(rows[0]["row_count"]),
                    "identifier_width": int(rows[0]["identifier_width"]),
                    "match_rate": float(rows[0]["match_rate"]),
                    "seed": int(rows[0]["seed"]),
                    "policy_id": policy.policy_id,
                    "allow_raw_join": policy.allow_raw_join,
                    "allow_raw_materialization": policy.allow_raw_materialization,
                    "legal_candidate_count": len(legal),
                    "legal_candidates": "|".join(sorted(row["variant"] for row in legal)),
                    "oracle_fastest_legal_variant": winner,
                    "practical_classification": classification,
                    "near_optimal_variants": near,
                    "winner_latency_ms": winner_ms,
                    "unconstrained_fastest_latency_ms": unconstrained,
                    "governance_overhead_percent": (winner_ms / unconstrained - 1.0) * 100.0,
                    "selected_raw_join_rows": int(winner_row["raw_rows_exposed_to_join"]),
                    "selected_raw_materialized_rows": int(winner_row["raw_rows_materialized"]),
                    "selected_masked_materialized_rows": int(
                        winner_row["masked_rows_materialized"]
                    ),
                    "selected_candidate_is_legal": variant_is_legal(winner_row, policy),
                }
            )
    return output


def _family_policy_rows(
    unit_rows: list[dict[str, Any]], required_seed_agreement_fraction: float
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in unit_rows:
        grouped.setdefault((str(row["family_id"]), str(row["policy_id"])), []).append(row)
    output: list[dict[str, Any]] = []
    for (family_id, policy_id), rows in sorted(grouped.items()):
        required = math.ceil(required_seed_agreement_fraction * len(rows))
        classes = [str(row["practical_classification"]) for row in rows]
        winner_counts = {
            value: classes.count(value)
            for value in (*PIPELINE_ABLATION_VARIANTS, "tie", "single_legal_candidate")
        }
        stable = next(
            (
                variant
                for variant in PIPELINE_ABLATION_VARIANTS
                if winner_counts[variant] >= required
            ),
            None,
        )
        if stable is None and winner_counts["single_legal_candidate"] >= required:
            stable = str(rows[0]["oracle_fastest_legal_variant"])
        family_classification = stable or (
            "stable_tie" if winner_counts["tie"] >= required else "mixed"
        )
        output.append(
            {
                "family_id": family_id,
                "policy_id": policy_id,
                "region_label": rows[0]["region_label"],
                "row_count": rows[0]["row_count"],
                "identifier_width": rows[0]["identifier_width"],
                "match_rate": rows[0]["match_rate"],
                "seed_count": len(rows),
                "required_seed_agreement_count": required,
                **{f"{name}_count": count for name, count in winner_counts.items()},
                "family_classification": family_classification,
                "median_governance_overhead_percent": statistics.median(
                    float(row["governance_overhead_percent"]) for row in rows
                ),
                "p95_governance_overhead_percent": sorted(
                    float(row["governance_overhead_percent"]) for row in rows
                )[math.ceil(0.95 * len(rows)) - 1],
            }
        )
    return output


def _report(summary: dict[str, Any], families: list[dict[str, Any]]) -> str:
    lines = [
        "# Phase 2M compact policy-aware ablation",
        "",
        "This is development evidence, not Phase 2G or a final optimizer result.",
        "",
        f"- Scenarios: {summary['scenario_count']}",
        f"- Families: {summary['family_count']}",
        f"- Timed measurements: {summary['measurement_count']}",
        f"- Policy changes stable optimum: {summary['policy_changes_stable_optimum']}",
        f"- V2.1 hypothesis gate: {summary['v2_1_hypothesis_gate']['passes']}",
        "",
        "| Region | Policy | Stable practical winner | Median governance overhead |",
        "|---|---|---|---:|",
    ]
    for row in families:
        lines.append(
            "| {region} | {policy} | {winner} | {overhead:.2f}% |".format(
                region=row["region_label"],
                policy=row["policy_id"],
                winner=row["family_classification"],
                overhead=row["median_governance_overhead_percent"],
            )
        )
    lines.extend(
        [
            "",
            "> Raw-intermediate materialization is a hard policy constraint. An ",
            "> illegal diagnostic is excluded before the legal oracle is computed.",
        ]
    )
    return "\n".join(lines) + "\n"


def analyze_compact_pipeline_ablation(
    run_dir_value: str | Path,
    output_dir_value: str | Path,
    *,
    tie_threshold_fraction: float = 0.03,
    required_seed_agreement_fraction: float = 0.8,
) -> Path:
    """Analyze policy-dependent legal optima under the frozen Phase 2M rules."""

    if not 0.0 <= tie_threshold_fraction < 1.0:
        raise ValueError("tie_threshold_fraction must be in [0, 1)")
    if not 0.5 < required_seed_agreement_fraction <= 1.0:
        raise ValueError("required seed agreement must be a strict majority")
    run_dir = Path(run_dir_value).resolve()
    summary = _read_object(run_dir / "summary.json")
    scenario_count = int(summary.get("scenario_count", -1))
    if (
        summary.get("status") != "complete"
        or summary.get("all_validations_passed") is not True
        or int(summary.get("result_equivalent_scenario_count", -2)) != scenario_count
        or int(summary.get("distinct_plan_scenario_count", -3)) != scenario_count
        or int(summary.get("boundary_validated_scenario_count", -4)) != scenario_count
        or int(summary.get("exact_join_cardinality_scenario_count", -5)) != scenario_count
    ):
        raise ValueError("Compact Phase 2M source run is incomplete or invalid")
    if int(summary.get("spilled_scenario_count", -1)) != 0:
        raise ValueError("Compact Phase 2M source run contains spilled scenarios")
    components = _read_csv(run_dir / "component_summary.csv")
    unit_rows = _unit_policy_rows(components, tie_threshold_fraction)
    families = _family_policy_rows(unit_rows, required_seed_agreement_fraction)
    family_ids = sorted({str(row["family_id"]) for row in families})
    policies = {str(row["policy_id"]) for row in families}
    expected_policies = {item.policy_id for item in ABLATION_POLICY_PROFILES}
    if policies != expected_policies:
        raise ValueError("Compact Phase 2M policy coverage is incomplete")
    seed_counts = {int(row["seed_count"]) for row in families}
    complete_five_seed_families = seed_counts == {5}
    family_map = {
        (str(row["family_id"]), str(row["policy_id"])): str(row["family_classification"])
        for row in families
    }
    changed_family_ids = [
        family_id
        for family_id in family_ids
        if len({family_map[(family_id, policy.policy_id)] for policy in ABLATION_POLICY_PROFILES})
        > 1
    ]
    no_raw_materialization_winners = {
        family_map[(family_id, "no_raw_materialization")]
        for family_id in family_ids
        if family_map[(family_id, "no_raw_materialization")] not in {"mixed", "stable_tie"}
    }
    all_choices_legal = all(bool(row["selected_candidate_is_legal"]) for row in unit_rows)
    checks = {
        "complete_five_seed_families": complete_five_seed_families,
        "at_least_one_policy_changes_stable_optimum": bool(changed_family_ids),
        "no_raw_materialization_has_multiple_stable_winners": (
            len(no_raw_materialization_winners) >= 2
        ),
        "all_selected_candidates_are_legal": all_choices_legal,
        "no_spilled_scenarios": int(summary["spilled_scenario_count"]) == 0,
    }
    passes = all(checks.values())
    analysis_summary: dict[str, Any] = {
        "evaluation_label": "phase2m_compact_policy_aware_ablation_development",
        "source_run_id": str(summary["run_id"]),
        "source_commit_hash": str(
            _read_object(run_dir / "environment.json").get("commit_hash", "unknown")
        ),
        "scenario_count": scenario_count,
        "family_count": len(family_ids),
        "policy_profile_count": len(ABLATION_POLICY_PROFILES),
        "measurement_count": int(summary["measurement_count"]),
        "tie_threshold_fraction": tie_threshold_fraction,
        "required_seed_agreement_fraction": required_seed_agreement_fraction,
        "policy_changes_stable_optimum": bool(changed_family_ids),
        "policy_changed_family_ids": changed_family_ids,
        "no_raw_materialization_stable_winners": sorted(no_raw_materialization_winners),
        "v2_1_hypothesis_gate": {"passes": passes, "checks": checks},
        "phase2g_authorized": False,
        "scientific_boundary": (
            "The compact matrix identifies policy-dependent legal oracle plans and "
            "governance overhead on three development families. It may authorize a "
            "versioned V2.1 hypothesis, not a final generalization claim."
        ),
    }
    output = Path(output_dir_value).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "policy_unit_summary.csv", unit_rows)
    _write_csv(output / "policy_family_summary.csv", families)
    _write_json(output / "summary.json", analysis_summary)
    (output / "report.md").write_text(_report(analysis_summary, families), encoding="utf-8")
    return output
