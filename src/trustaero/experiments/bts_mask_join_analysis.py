"""Stability gates for the non-paper BTS Mask/Join paired protocol."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from trustaero.experiments.bts_mask_join_pilot import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    MASK_JOIN_FORMAL_LABEL,
    MASK_JOIN_PILOT_LABEL,
)
from trustaero.experiments.real_data_governed import _atomic_json


def _half_drift(values: list[float]) -> float:
    midpoint = len(values) // 2
    if midpoint == 0:
        return 0.0
    first = statistics.median(values[:midpoint])
    second = statistics.median(values[midpoint:])
    return abs(second / first - 1.0)


def _ratio_outlier_fraction(values: list[float]) -> float:
    center = statistics.median(values)
    deviations = [abs(value - center) for value in values]
    mad = statistics.median(deviations)
    threshold = max(3.0 * mad, 0.15)
    return sum(value > threshold for value in deviations) / len(values)


def analyze_bts_mask_join_pilot(run_dir: Path) -> dict[str, Any]:
    """Apply predeclared paired-order and stability checks."""

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    is_formal = summary["scientific_label"] == MASK_JOIN_FORMAL_LABEL
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    by_candidate: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_block: dict[int, dict[str, float]] = defaultdict(dict)
    for row in rows:
        by_candidate[row["candidate_id"]].append(row)
        by_block[int(row["block_index"])][row["candidate_id"]] = float(
            row["client_materialization_latency_ms"]
        )
    expected_candidates = {LATE_CANDIDATE, EARLY_CANDIDATE}
    expected_blocks = int(config["measured_blocks"])
    permutation_counts = Counter(
        next(iter(block_rows))["permutation_id"]
        for _, block_rows in sorted(
            (
                index,
                [row for row in rows if int(row["block_index"]) == index],
            )
            for index in by_block
        )
    )
    position_counts = {
        candidate: Counter(int(row["order_position"]) for row in candidate_rows)
        for candidate, candidate_rows in by_candidate.items()
    }
    ordered_latencies = {
        candidate: [
            float(row["client_materialization_latency_ms"])
            for row in sorted(candidate_rows, key=lambda item: int(item["block_index"]))
        ]
        for candidate, candidate_rows in by_candidate.items()
    }
    ratios = [
        values[EARLY_CANDIDATE] / values[LATE_CANDIDATE] for _, values in sorted(by_block.items())
    ]
    absolute_drift = {
        candidate: _half_drift(values) for candidate, values in ordered_latencies.items()
    }
    ratio_drift = _half_drift(ratios)
    outlier_fraction = _ratio_outlier_fraction(ratios)
    absolute_limit = float(config["absolute_half_drift_limit"])
    ratio_limit = float(config["paired_ratio_half_drift_limit"])
    outlier_limit = float(config["paired_ratio_outlier_fraction_limit"])
    integrity_gates = {
        "summary_pass": summary["status"] == "PASS",
        "scientific_boundary_preserved": (
            (
                summary["scientific_label"] == MASK_JOIN_PILOT_LABEL
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
        "candidate_space_complete": (
            int(summary["candidate_count"]) == 2
            and int(summary["distinct_duckdb_plan_count"]) == 2
            and set(summary["candidate_summaries"]) == expected_candidates
        ),
        "measurements_complete": len(rows) == 2 * expected_blocks,
        "paired_blocks_complete": (
            len(by_block) == expected_blocks
            and all(set(values) == expected_candidates for values in by_block.values())
        ),
        "both_permutations_balanced": (
            len(permutation_counts) == 2
            and set(permutation_counts.values()) == {expected_blocks // 2}
        ),
        "positions_balanced": all(
            set(counts) == {0, 1} and set(counts.values()) == {expected_blocks // 2}
            for counts in position_counts.values()
        ),
        "artifacts_verified": len(summary["verified_execution_artifacts"]) == 2,
        "certificates_partial": all(
            item["certificate_status"] == "PARTIAL"
            for item in summary["candidate_summaries"].values()
        ),
        "resources_observed": all(
            int(item["peak_buffer_memory_bytes"]) > 0
            and int(item["peak_temp_directory_bytes"]) >= 0
            for item in summary["candidate_summaries"].values()
        ),
        "strict_policy_forces_early": (
            summary["governance_profiles"]["no-raw-sensitive-join"]["feasible_candidate_ids"]
            == [EARLY_CANDIDATE]
        ),
        "source_worktree_recorded": isinstance(environment.get("git_dirty"), bool),
    }
    stability_gates = {
        "absolute_half_drift": max(absolute_drift.values(), default=0.0) <= absolute_limit,
        "paired_ratio_half_drift": ratio_drift <= ratio_limit,
        "paired_ratio_outlier_fraction": outlier_fraction <= outlier_limit,
    }
    candidates = summary["candidate_summaries"]
    medians = {candidate: float(item["median_ms"]) for candidate, item in candidates.items()}
    tie = float(config["tie_threshold_fraction"])
    median_ratio = statistics.median(ratios)
    # The experiment is paired, so its tie decision must also be paired. Using
    # separate candidate medians here would reintroduce whole-run drift that
    # the block design was created to remove.
    if median_ratio < 1.0 / (1.0 + tie):
        oracle_set = [EARLY_CANDIDATE]
    elif median_ratio > 1.0 + tie:
        oracle_set = [LATE_CANDIDATE]
    else:
        oracle_set = sorted(expected_candidates)
    all_pass = all(integrity_gates.values()) and all(stability_gates.values())
    payload = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "status": "PASS" if all_pass else "FAIL",
        "scientific_label": summary["scientific_label"],
        "paper_performance_evidence": bool(summary["paper_performance_evidence"]),
        "heldout_optimizer_evidence": bool(summary.get("heldout_optimizer_evidence", False)),
        "formal_paper_experiment_authorized": is_formal and all_pass,
        "paired_protocol_stable_for_future_clean_run": all_pass,
        "source_worktree_dirty": bool(environment["git_dirty"]),
        "data_scope": "full_month" if summary.get("full_month", False) else "slice",
        "sample_rows": int(summary["sample_rows"]),
        "tie_threshold_fraction": tie,
        "integrity_gates": integrity_gates,
        "stability_gates": stability_gates,
        "absolute_half_drift_by_candidate": absolute_drift,
        "paired_early_over_late_ratio_half_drift": ratio_drift,
        "paired_ratio_outlier_fraction": outlier_fraction,
        "median_latency_ms": medians,
        "median_early_over_late_ratio": median_ratio,
        "diagnostic_oracle_set_within_tie_band": oracle_set,
        "strict_policy_feasible_set": [EARLY_CANDIDATE],
        "optimizer_selection_evaluated": False,
        "scientific_boundary": (
            "This run evaluates a frozen query on the January development partition. "
            "It may support method-level paper analysis when the formal gates pass, but it "
            "does not evaluate Optimizer V1/V2 and is not independent held-out evidence."
            if is_formal
            else (
                "This run validates a paired timing method on the frozen data scope. "
                "It is not paper evidence, does not evaluate Optimizer V1/V2, and cannot be "
                "upgraded after the fact. A clean separately frozen run is required."
            )
        ),
    }
    _atomic_json(run_dir / "acceptance.json", payload)
    _write_report(run_dir / "report.md", summary, payload)
    return payload


def _write_report(path: Path, summary: dict[str, Any], analysis: dict[str, Any]) -> None:
    lines = [
        "# BTS Mask/Join paired timing-protocol validation",
        "",
        f"Status: **{analysis['status']}**",
        "",
        (
            f"This is a formal development-partition {analysis['data_scope']} run over "
            f"{analysis['sample_rows']} rows, not held-out optimizer evidence."
            if analysis["paper_performance_evidence"]
            else f"This is a non-paper {analysis['data_scope']} run over "
            f"{analysis['sample_rows']} rows, not an optimizer result."
        ),
        "",
        "| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) | Spill (MiB) |",
        "|---|---:|---:|---:|---:|",
    ]
    for candidate, values in summary["candidate_summaries"].items():
        lines.append(
            f"| {candidate} | {values['median_ms']:.3f} | {values['p95_ms']:.3f} | "
            f"{values['peak_buffer_memory_bytes'] / 1048576:.2f} | "
            f"{values['peak_temp_directory_bytes'] / 1048576:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Stability and governance boundary",
            "",
            f"- Paired protocol stable: "
            f"`{analysis['paired_protocol_stable_for_future_clean_run']}`",
            f"- Median early/late ratio: `{analysis['median_early_over_late_ratio']:.4f}`",
            f"- Paired 3% tie-band set: `{analysis['diagnostic_oracle_set_within_tie_band']}`",
            f"- Strict policy feasible set: `{analysis['strict_policy_feasible_set']}`",
            f"- Source worktree dirty: `{analysis['source_worktree_dirty']}`",
            f"- Formal paper experiment authorized: "
            f"`{analysis['formal_paper_experiment_authorized']}`.",
            "- No optimizer selection was evaluated.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
