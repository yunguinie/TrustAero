"""Integrity analysis for the approved multi-candidate real-data pilot."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from trustaero.experiments.real_data_candidate_pilot import FORMAL_CANDIDATE_LABEL, PILOT_LABEL


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def analyze_real_data_candidate_pilot(
    run_dir: Path,
    *,
    tie_threshold_fraction: float = 0.03,
) -> dict[str, Any]:
    """Apply integrity gates and summarize diagnostic Oracle opportunities."""

    if not 0.0 <= tie_threshold_fraction < 1.0:
        raise ValueError("tie threshold must be in [0, 1)")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    is_formal = summary["scientific_label"] == FORMAL_CANDIDATE_LABEL
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        measurements = list(csv.DictReader(handle))
    by_unit: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in measurements:
        by_unit[row["unit_id"]].append(row)

    unit_gates: dict[str, dict[str, bool]] = {}
    timing_drift_by_unit: dict[str, float] = {}
    paired_ratio_drift_by_unit: dict[str, float] = {}
    paired_ratio_outlier_fraction_by_unit: dict[str, float] = {}
    observations: list[dict[str, Any]] = []
    oracle_by_workload_profile: dict[tuple[str, str], list[str]] = defaultdict(list)
    for unit in summary["units"]:
        unit_id = str(unit["unit_id"])
        rows = by_unit[unit_id]
        candidates = unit["candidate_summaries"]
        expected_measurements = 3 * 5
        # The frozen config remains authoritative when a test uses fewer runs.
        expected_measurements = 3 * int(config["measured_runs"])
        position_counts: dict[str, Counter[int]] = defaultdict(Counter)
        for row in rows:
            position_counts[row["strategy_id"]][int(row["order_position"])] += 1
        balanced = all(
            max(counts.values(), default=0) - min(counts.values(), default=0) <= 1
            and set(counts) == {0, 1, 2}
            for counts in position_counts.values()
        )
        strategy_drifts: list[float] = []
        for strategy_id in candidates:
            ordered_rows = sorted(
                (row for row in rows if row["strategy_id"] == strategy_id),
                key=lambda row: int(row["repeat_index"]),
            )
            midpoint = len(ordered_rows) // 2
            if midpoint:
                first = statistics.median(
                    float(row["client_materialization_latency_ms"])
                    for row in ordered_rows[:midpoint]
                )
                second = statistics.median(
                    float(row["client_materialization_latency_ms"])
                    for row in ordered_rows[midpoint:]
                )
                strategy_drifts.append(abs(second / first - 1.0))
        timing_drift_by_unit[unit_id] = max(strategy_drifts, default=0.0)
        by_repeat: dict[int, dict[str, float]] = defaultdict(dict)
        for row in rows:
            by_repeat[int(row["repeat_index"])][row["strategy_id"]] = float(
                row["client_materialization_latency_ms"]
            )
        ratio_drifts: list[float] = []
        ratio_outlier_fractions: list[float] = []
        for strategy_id in candidates:
            if strategy_id == "fused":
                continue
            ratios = [
                values[strategy_id] / values["fused"] for _, values in sorted(by_repeat.items())
            ]
            midpoint = len(ratios) // 2
            if midpoint:
                first = statistics.median(ratios[:midpoint])
                second = statistics.median(ratios[midpoint:])
                ratio_drifts.append(abs(second / first - 1.0))
            center = statistics.median(ratios)
            deviations = [abs(value - center) for value in ratios]
            mad = statistics.median(deviations)
            outlier_threshold = max(3.0 * mad, 0.15)
            ratio_outlier_fractions.append(
                sum(value > outlier_threshold for value in deviations) / len(ratios)
            )
        paired_ratio_drift_by_unit[unit_id] = max(ratio_drifts, default=0.0)
        paired_ratio_outlier_fraction_by_unit[unit_id] = max(
            ratio_outlier_fractions,
            default=0.0,
        )
        permutation_gate = True
        if config.get("order_protocol", "cyclic") == "all_permutations":
            blocks = {(int(row["repeat_index"]), row["permutation_id"]) for row in rows}
            permutation_counts = Counter(permutation for _, permutation in blocks)
            permutation_gate = len(permutation_counts) == 6 and set(
                permutation_counts.values()
            ) == {int(config["measured_runs"]) // 6}
        unit_gates[unit_id] = {
            "status_pass": unit["status"] == "PASS",
            "candidate_space_complete": (
                int(unit["candidate_count"]) == 3
                and int(unit["distinct_duckdb_plan_count"]) == 3
                and len(candidates) == 3
            ),
            "measurements_complete": len(rows) == expected_measurements,
            "execution_order_balanced": balanced,
            "all_permutations_complete": permutation_gate,
            "artifacts_verified": len(unit["verified_execution_artifacts"])
            == ((1 if unit["workload"] == "bts" else 2) if unit.get("full_month", False) else 3),
            "certificates_partial": all(
                item["certificate_status"] == "PARTIAL" for item in candidates.values()
            ),
            "physical_resources_observed": all(
                int(item["peak_buffer_memory_bytes"]) > 0
                and int(item["peak_temp_directory_bytes"]) >= 0
                for item in candidates.values()
            ),
            "latencies_positive": all(
                0.0
                < float(item["min_ms"])
                <= float(item["median_ms"])
                <= float(item["p95_ms"])
                <= float(item["max_ms"])
                for item in candidates.values()
            ),
        }
        for profile_id, profile in unit["policy_profiles"].items():
            feasible = list(profile["feasible_candidate_ids"])
            best = min(float(candidates[item]["median_ms"]) for item in feasible)
            oracle_set = [
                item
                for item in feasible
                if float(candidates[item]["median_ms"]) <= best * (1.0 + tie_threshold_fraction)
            ]
            oracle_by_workload_profile[(unit["workload"], profile_id)].append(
                "|".join(sorted(oracle_set))
            )
            observations.append(
                {
                    "unit_id": unit_id,
                    "policy_profile": profile_id,
                    "feasible_candidate_ids": feasible,
                    "rejected_candidate_ids": profile["rejected_candidate_ids"],
                    "oracle_set_within_3_percent": oracle_set,
                    "best_median_ms": best,
                    "fixed_fused_median_ms": float(profile["fixed_fused_median_ms"]),
                    "oracle_opportunity_speedup_vs_fused": float(profile["fixed_fused_median_ms"])
                    / best,
                }
            )

    global_gates = {
        "all_units_complete": int(summary["completed_units"])
        == int(summary["expected_units"])
        == len(summary["units"]),
        "scientific_boundary_preserved": (
            (
                summary["scientific_label"] == PILOT_LABEL
                and summary["paper_performance_evidence"] is False
            )
            or (
                is_formal
                and summary["paper_performance_evidence"] is True
                and summary.get("heldout_optimizer_evidence") is False
                and environment.get("git_dirty") is False
            )
        )
        and summary["optimizer_selection_evaluated"] is False,
        "all_unit_integrity_gates_pass": all(
            value for gates in unit_gates.values() for value in gates.values()
        ),
    }
    all_passed = all(global_gates.values())
    absolute_limit = float(config.get("absolute_half_drift_limit", 0.25))
    paired_drift_limit = float(config.get("paired_ratio_half_drift_limit", 0.20))
    outlier_limit = float(config.get("paired_ratio_outlier_fraction_limit", 0.10))
    absolute_stability_pass = all(
        value <= absolute_limit for value in timing_drift_by_unit.values()
    )
    paired_stability_pass = all(
        value <= paired_drift_limit for value in paired_ratio_drift_by_unit.values()
    ) and all(value <= outlier_limit for value in paired_ratio_outlier_fraction_by_unit.values())
    all_permutation_protocol = config.get("order_protocol") == "all_permutations"
    timing_stability_pass = absolute_stability_pass and paired_stability_pass
    policy_changes_oracle = any(
        next(
            observation["oracle_set_within_3_percent"]
            for observation in observations
            if observation["unit_id"] == unit["unit_id"]
            and observation["policy_profile"] == "output-mask-only"
        )
        != next(
            observation["oracle_set_within_3_percent"]
            for observation in observations
            if observation["unit_id"] == unit["unit_id"]
            and observation["policy_profile"] == "no-raw-sensitive-materialization"
        )
        for unit in summary["units"]
    )
    scale_reversal = any(len(set(values)) > 1 for values in oracle_by_workload_profile.values())
    payload = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "status": "PASS" if all_passed else "FAIL",
        "scientific_label": summary["scientific_label"],
        "paper_performance_evidence": bool(summary["paper_performance_evidence"]),
        "heldout_optimizer_evidence": bool(summary.get("heldout_optimizer_evidence", False)),
        "tie_threshold_fraction": tie_threshold_fraction,
        "full_month_preexperiment_authorized": all_passed,
        "formal_performance_experiment_authorized": is_formal
        and all_passed
        and timing_stability_pass
        and all_permutation_protocol,
        "order_protocol": config.get("order_protocol", "cyclic"),
        "absolute_half_run_drift_threshold": absolute_limit,
        "paired_ratio_half_drift_threshold": paired_drift_limit,
        "paired_ratio_outlier_fraction_threshold": outlier_limit,
        "absolute_timing_stability_pass": absolute_stability_pass,
        "paired_timing_stability_pass": paired_stability_pass,
        "timing_stability_pass": timing_stability_pass,
        "max_half_run_median_drift_by_unit": timing_drift_by_unit,
        "max_paired_ratio_half_drift_by_unit": paired_ratio_drift_by_unit,
        "max_paired_ratio_outlier_fraction_by_unit": (paired_ratio_outlier_fraction_by_unit),
        "policy_changes_legal_oracle_observed": policy_changes_oracle,
        "scale_dependent_oracle_reversal_observed": scale_reversal,
        "global_gates": global_gates,
        "unit_gates": unit_gates,
        "observations": observations,
    }
    _write_json(run_dir / "acceptance.json", payload)
    _write_report(run_dir / "report.md", summary, payload)
    return payload


def _write_report(path: Path, summary: dict[str, Any], analysis: dict[str, Any]) -> None:
    lines = [
        "# Approved real-data multi-candidate measurement",
        "",
        f"Integrity status: **{analysis['status']}**",
        "",
        (
            "This is a frozen development-partition paper-candidate measurement; "
            "it is not held-out optimizer evidence."
            if analysis["paper_performance_evidence"]
            else "These are diagnostic pilot timings, not paper evidence or an optimizer result."
        ),
        "",
        "| Unit | Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) | Spill (MiB) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for unit in summary["units"]:
        for strategy_id, candidate in unit["candidate_summaries"].items():
            lines.append(
                f"| {unit['unit_id']} | {strategy_id} | {candidate['median_ms']:.3f} | "
                f"{candidate['p95_ms']:.3f} | "
                f"{candidate['peak_buffer_memory_bytes'] / 1048576:.2f} | "
                f"{candidate['peak_temp_directory_bytes'] / 1048576:.2f} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            f"- Full-month pre-experiment authorized: "
            f"`{analysis['full_month_preexperiment_authorized']}`",
            f"- Formal performance experiment authorized: "
            f"`{analysis['formal_performance_experiment_authorized']}`",
            f"- Timing stability diagnostic passed: `{analysis['timing_stability_pass']}`",
            f"- Policy-dependent legal Oracle observed: "
            f"`{analysis['policy_changes_legal_oracle_observed']}`",
            f"- Scale-dependent Oracle reversal observed: "
            f"`{analysis['scale_dependent_oracle_reversal_observed']}`",
            "- Oracle is computed after running all legal candidates and is not deployable.",
            "- A 3% band is treated as a tie; no winner is claimed inside that band.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
