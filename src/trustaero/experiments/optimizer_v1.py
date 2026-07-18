"""Evaluate Mask Optimizer V1 against measured Phase 2E candidates."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, cast

from trustaero.optimizer.mask import (
    MaskPlacement,
    MaskPlacementFeatures,
    choose_mask_placement,
)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing optimizer input: {path.name}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _percentile(values: list[float], fraction: float) -> float:
    """Return a deterministic nearest-rank percentile for a small workload set."""

    if not values:
        raise ValueError("Cannot summarize an empty measurement")
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _geometric_mean(values: list[float]) -> float:
    """Return a geometric mean for positive latency ratios."""

    if not values or any(value <= 0.0 for value in values):
        raise ValueError("Geometric-mean inputs must be positive")
    return math.exp(statistics.mean(math.log(value) for value in values))


def _scenario_widths(config: dict[str, Any]) -> dict[str, int]:
    scenarios = config.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("Phase 2E config has no scenario list")
    widths: dict[str, int] = {}
    for item in scenarios:
        if not isinstance(item, dict):
            raise ValueError("Each Phase 2E scenario must be an object")
        scenario = cast(dict[str, Any], item)
        widths[str(scenario["scenario_id"])] = int(scenario["identifier_width"])
    return widths


def _classify_strategies(
    rows: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, dict[str, dict[str, str]]]]:
    by_unit: dict[str, dict[str, dict[str, str]]] = {}
    semantic_by_strategy: dict[str, str] = {}
    for row in rows:
        strategy_id = row["strategy_id"]
        exposure = int(row["raw_sensitive_rows_exposed_to_join"])
        semantic = MaskPlacement.EARLY if exposure == 0 else MaskPlacement.LATE
        prior = semantic_by_strategy.setdefault(strategy_id, semantic.value)
        if prior != semantic.value:
            raise ValueError(f"Strategy {strategy_id} changes Mask semantics across units")
        by_unit.setdefault(row["unit_id"], {})[semantic.value] = row
    for unit_id, choices in by_unit.items():
        if set(choices) != {MaskPlacement.EARLY.value, MaskPlacement.LATE.value}:
            raise ValueError(f"Unit {unit_id} does not contain exactly one early and one late Mask")
    return semantic_by_strategy, by_unit


def _case_rows(
    by_unit: dict[str, dict[str, dict[str, str]]],
    widths: dict[str, int],
    *,
    tie_threshold: float,
    max_raw_exposure_rows: int | None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for unit_id, choices in sorted(by_unit.items()):
        early = choices[MaskPlacement.EARLY.value]
        late = choices[MaskPlacement.LATE.value]
        scenario_id = late["scenario_id"]
        join_input = int(late["after_policy_rows"])
        join_output = int(late["after_join_rows"])
        match_rate = join_output / join_input if join_input else 0.0
        decision = choose_mask_placement(
            MaskPlacementFeatures(
                join_input_rows=join_input,
                identifier_width_bytes=widths[scenario_id],
                join_match_rate=match_rate,
                max_raw_exposure_rows=max_raw_exposure_rows,
            )
        )
        selected = choices[decision.placement.value]
        selected_ms = float(selected["median_governed_latency_ms"])
        early_ms = float(early["median_governed_latency_ms"])
        late_ms = float(late["median_governed_latency_ms"])
        oracle_ms = min(early_ms, late_ms)
        within_tie = selected_ms <= oracle_ms * (1.0 + tie_threshold)
        output.append(
            {
                "unit_id": unit_id,
                "scenario_id": scenario_id,
                "row_count": int(late["row_count"]),
                "data_seed": int(late["data_seed"]),
                "identifier_width_bytes": widths[scenario_id],
                "estimated_join_match_rate": match_rate,
                "selected_placement": decision.placement.value,
                "selected_strategy_id": selected["strategy_id"],
                "decision_reason_code": decision.reason_code,
                "early_proxy_work_bytes": decision.early_proxy_work_bytes,
                "late_proxy_work_bytes": decision.late_proxy_work_bytes,
                "early_latency_ms": early_ms,
                "late_latency_ms": late_ms,
                "selected_latency_ms": selected_ms,
                "oracle_latency_ms": oracle_ms,
                "exact_top1": math.isclose(selected_ms, oracle_ms),
                "within_tie_threshold": within_tie,
                "regret_percent": (selected_ms / oracle_ms - 1.0) * 100.0,
                "speedup_vs_fixed_late_percent": (late_ms / selected_ms - 1.0) * 100.0,
                "speedup_vs_fixed_early_percent": (early_ms / selected_ms - 1.0) * 100.0,
                "fixed_early_regret_percent": (early_ms / oracle_ms - 1.0) * 100.0,
                "fixed_late_regret_percent": (late_ms / oracle_ms - 1.0) * 100.0,
                "selected_raw_exposure_rows": int(selected["raw_sensitive_rows_exposed_to_join"]),
                "selected_mask_rows_processed": int(selected["mask_rows_processed"]),
            }
        )
    return output


def _summarize(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    evaluation_label: str,
    tie_threshold: float,
    max_raw_exposure_rows: int | None,
) -> dict[str, Any]:
    regrets = [float(row["regret_percent"]) for row in rows]
    late_speedups = [float(row["speedup_vs_fixed_late_percent"]) for row in rows]
    early_speedups = [float(row["speedup_vs_fixed_early_percent"]) for row in rows]
    return {
        "source_run_id": run_id,
        "evaluation_label": evaluation_label,
        "case_count": len(rows),
        "exact_top1_count": sum(bool(row["exact_top1"]) for row in rows),
        "exact_top1_rate": sum(bool(row["exact_top1"]) for row in rows) / len(rows),
        "within_tie_count": sum(bool(row["within_tie_threshold"]) for row in rows),
        "within_tie_rate": sum(bool(row["within_tie_threshold"]) for row in rows) / len(rows),
        "median_regret_percent": statistics.median(regrets),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _percentile(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "median_speedup_vs_fixed_late_percent": statistics.median(late_speedups),
        "p95_speedup_vs_fixed_late_percent": _percentile(late_speedups, 0.95),
        "median_speedup_vs_fixed_early_percent": statistics.median(early_speedups),
        "geometric_speedup_vs_fixed_late_ratio": _geometric_mean(
            [float(row["late_latency_ms"]) / float(row["selected_latency_ms"]) for row in rows]
        ),
        "geometric_speedup_vs_fixed_early_ratio": _geometric_mean(
            [float(row["early_latency_ms"]) / float(row["selected_latency_ms"]) for row in rows]
        ),
        "total_latency_speedup_vs_fixed_late_ratio": sum(
            float(row["late_latency_ms"]) for row in rows
        )
        / sum(float(row["selected_latency_ms"]) for row in rows),
        "total_latency_speedup_vs_fixed_early_ratio": sum(
            float(row["early_latency_ms"]) for row in rows
        )
        / sum(float(row["selected_latency_ms"]) for row in rows),
        "median_fixed_early_regret_percent": statistics.median(
            float(row["fixed_early_regret_percent"]) for row in rows
        ),
        "median_fixed_late_regret_percent": statistics.median(
            float(row["fixed_late_regret_percent"]) for row in rows
        ),
        "early_selection_count": sum(
            row["selected_placement"] == MaskPlacement.EARLY.value for row in rows
        ),
        "late_selection_count": sum(
            row["selected_placement"] == MaskPlacement.LATE.value for row in rows
        ),
        "total_selected_raw_exposure_rows": sum(
            int(row["selected_raw_exposure_rows"]) for row in rows
        ),
        "raw_exposure_limit_rows": max_raw_exposure_rows,
        "tie_threshold_fraction": tie_threshold,
        "interpretation_limit": (
            "Calibration/resubstitution results do not measure generalization. Use an unseen "
            "workload configuration for a held-out optimizer claim."
            if evaluation_label == "calibration"
            else "Held-out status is valid only if this configuration was not used to tune V1."
        ),
    }


def _scenario_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate independent seeds without hiding scenario-specific failures."""

    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row["scenario_id"]), int(row["row_count"])), []).append(row)
    output: list[dict[str, Any]] = []
    for (scenario_id, row_count), group in sorted(grouped.items()):
        placements = {str(row["selected_placement"]) for row in group}
        output.append(
            {
                "scenario_id": scenario_id,
                "row_count": row_count,
                "seed_count": len(group),
                "selected_placement": ",".join(sorted(placements)),
                "exact_top1_count": sum(bool(row["exact_top1"]) for row in group),
                "within_tie_count": sum(bool(row["within_tie_threshold"]) for row in group),
                "median_regret_percent": statistics.median(
                    float(row["regret_percent"]) for row in group
                ),
                "geometric_speedup_vs_fixed_late_ratio": _geometric_mean(
                    [
                        float(row["late_latency_ms"]) / float(row["selected_latency_ms"])
                        for row in group
                    ]
                ),
                "geometric_speedup_vs_fixed_early_ratio": _geometric_mean(
                    [
                        float(row["early_latency_ms"]) / float(row["selected_latency_ms"])
                        for row in group
                    ]
                ),
                "median_selected_raw_exposure_rows": statistics.median(
                    int(row["selected_raw_exposure_rows"]) for row in group
                ),
            }
        )
    return output


def _report(summary: dict[str, Any], scenarios: list[dict[str, Any]]) -> str:
    lines = [
        "# Mask Optimizer V1 evaluation",
        "",
        f"Evaluation label: **{summary['evaluation_label']}**  \n"
        f"Cases: **{summary['case_count']}**  \n"
        f"Exact top-1 rate: **{summary['exact_top1_rate']:.1%}**  \n"
        f"Within-tie rate: **{summary['within_tie_rate']:.1%}**  \n"
        f"Median regret: **{summary['median_regret_percent']:.2f}%**  \n"
        f"Mean regret: **{summary['mean_regret_percent']:.2f}%**  \n"
        f"P95 regret: **{summary['p95_regret_percent']:.2f}%**  \n"
        "Geometric-mean speedup versus fixed late Mask: "
        f"**{summary['geometric_speedup_vs_fixed_late_ratio']:.3f}x**  \n"
        "Geometric-mean speedup versus fixed early Mask: "
        f"**{summary['geometric_speedup_vs_fixed_early_ratio']:.3f}x**",
        "",
        "| Scenario | Rows | Choice | Exact | Within 3% | Median regret |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for row in scenarios:
        lines.append(
            "| {scenario_id} | {row_count} | {selected_placement} | "
            "{exact_top1_count}/{seed_count} | {within_tie_count}/{seed_count} | "
            "{median_regret_percent:.2f}% |".format(**row)
        )
    lines.extend(["", f"> {summary['interpretation_limit']}"])
    return "\n".join(lines) + "\n"


def evaluate_mask_optimizer_v1(
    run_dir: str | Path,
    *,
    evaluation_label: str,
    max_raw_exposure_rows: int | None = None,
) -> Path:
    """Evaluate V1 on a completed, result-equivalent Phase 2E run."""

    if evaluation_label not in {"calibration", "held_out"}:
        raise ValueError("evaluation_label must be 'calibration' or 'held_out'")
    resolved = Path(run_dir).resolve()
    run_summary = _read_object(resolved / "summary.json")
    if run_summary.get("status") != "complete":
        raise ValueError("Optimizer evaluation requires a complete source run")
    if run_summary.get("all_results_equivalent") is not True:
        raise ValueError("Optimizer evaluation requires equivalent candidate results")
    config = _read_object(resolved / "config.json")
    strategy_rows = _read_rows(resolved / "strategy_summary.csv")
    if not strategy_rows:
        raise ValueError("strategy_summary.csv is empty")
    _, by_unit = _classify_strategies(strategy_rows)
    tie_threshold = float(run_summary["tie_threshold_fraction"])
    rows = _case_rows(
        by_unit,
        _scenario_widths(config),
        tie_threshold=tie_threshold,
        max_raw_exposure_rows=max_raw_exposure_rows,
    )
    summary = _summarize(
        rows,
        run_id=str(run_summary.get("run_id", resolved.name)),
        evaluation_label=evaluation_label,
        tie_threshold=tie_threshold,
        max_raw_exposure_rows=max_raw_exposure_rows,
    )
    scenarios = _scenario_summaries(rows)
    output = resolved / "optimizer_v1_analysis"
    _write_rows(output / "cases.csv", rows)
    _write_rows(output / "scenario_summary.csv", scenarios)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "report.md").write_text(_report(summary, scenarios), encoding="utf-8")
    return output
