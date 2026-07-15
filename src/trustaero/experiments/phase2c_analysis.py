"""Paired, seed-level analysis for completed Phase 2C runs.

The timing repetitions inside one data seed share the same generated database,
so they are not treated as independent paper samples.  This module compares
strategies within each seed first, then bootstraps those paired seed summaries.
Raw runner artifacts are read-only; every derived file is written below the
run's ``analysis`` directory.
"""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing Phase 2C artifact: {path.name}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _bootstrap_median_interval(
    values: tuple[float, ...],
    *,
    rng: random.Random,
    bootstrap_runs: int,
) -> tuple[float, float]:
    """Return a deterministic percentile interval over independent seeds."""

    if not values:
        raise ValueError("Cannot bootstrap an empty seed sample")
    estimates = sorted(
        statistics.median(rng.choice(values) for _ in values) for _ in range(bootstrap_runs)
    )
    lower = estimates[round(0.025 * (len(estimates) - 1))]
    upper = estimates[round(0.975 * (len(estimates) - 1))]
    return lower, upper


def _classify_pair(speedup_ratio: float, tie_threshold: float) -> str:
    """Classify one candidate against fused using the frozen 3% convention."""

    if speedup_ratio - 1.0 > tie_threshold:
        return "win"
    if (1.0 / speedup_ratio) - 1.0 > tie_threshold:
        return "loss"
    return "tie"


def _validate_complete_run(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise ValueError("Phase 2C summary.json is missing")
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Phase 2C summary.json must contain a JSON object")
    summary = cast(dict[str, Any], payload)
    if summary.get("status") != "complete":
        raise ValueError("Only a complete Phase 2C run can be analyzed")
    if summary.get("all_results_equivalent") is not True:
        raise ValueError("Cannot compare timings when candidate results are not equivalent")
    return summary


def _paired_rows(
    strategy_rows: list[dict[str, str]],
    *,
    tie_threshold: float,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, int], dict[str, dict[str, str]]] = {}
    for row in strategy_rows:
        key = (str(row["scenario_id"]), int(row["row_count"]), int(row["data_seed"]))
        strategy_id = str(row["strategy_id"])
        if strategy_id in grouped.setdefault(key, {}):
            raise ValueError(f"Duplicate strategy summary for unit {key}: {strategy_id}")
        grouped[key][strategy_id] = row

    comparisons: list[dict[str, Any]] = []
    for (scenario_id, row_count, data_seed), by_strategy in sorted(grouped.items()):
        if "fused" not in by_strategy:
            raise ValueError(f"Unit {(scenario_id, row_count, data_seed)} has no fused baseline")
        latencies = {
            strategy_id: float(row["median_governed_latency_ms"])
            for strategy_id, row in by_strategy.items()
        }
        if any(value <= 0.0 for value in latencies.values()):
            raise ValueError("Governed latency must be positive")
        fused_ms = latencies["fused"]
        oracle_ms = min(latencies.values())
        for strategy_id, candidate_ms in sorted(latencies.items()):
            speedup_ratio = fused_ms / candidate_ms
            comparisons.append(
                {
                    "scenario_id": scenario_id,
                    "row_count": row_count,
                    "data_seed": data_seed,
                    "strategy_id": strategy_id,
                    "candidate_median_ms": candidate_ms,
                    "fused_median_ms": fused_ms,
                    "speedup_vs_fused_ratio": speedup_ratio,
                    "speedup_vs_fused_percent": (speedup_ratio - 1.0) * 100.0,
                    "regret_vs_oracle_percent": (candidate_ms / oracle_ms - 1.0) * 100.0,
                    "paired_outcome": _classify_pair(speedup_ratio, tie_threshold),
                    "is_oracle_for_seed": math.isclose(candidate_ms, oracle_ms),
                }
            )
    return comparisons


def _group_by(
    rows: Iterable[dict[str, Any]],
    keys: tuple[str, ...],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(tuple(row[key] for key in keys), []).append(row)
    return grouped


def _strategy_comparisons(
    paired_rows: list[dict[str, Any]],
    *,
    tie_threshold: float,
    bootstrap_seed: int,
    bootstrap_runs: int,
    minimum_stable_seeds: int,
) -> list[dict[str, Any]]:
    grouped = _group_by(paired_rows, ("scenario_id", "row_count", "strategy_id"))
    output: list[dict[str, Any]] = []
    for group_index, (key, rows) in enumerate(sorted(grouped.items())):
        scenario_id, row_count, strategy_id = key
        speedups = tuple(float(row["speedup_vs_fused_percent"]) for row in rows)
        rng = random.Random(bootstrap_seed + group_index)
        lower, upper = _bootstrap_median_interval(
            speedups,
            rng=rng,
            bootstrap_runs=bootstrap_runs,
        )
        outcomes = [str(row["paired_outcome"]) for row in rows]
        win_count = outcomes.count("win")
        seed_count = len(rows)
        median_speedup = statistics.median(speedups)
        # A stable reversal must be practically meaningful, positive under the
        # paired interval, and repeat in at least 80% of independent seeds.
        stable = (
            strategy_id != "fused"
            and seed_count >= minimum_stable_seeds
            and median_speedup > tie_threshold * 100.0
            and lower > 0.0
            and win_count >= math.ceil(0.8 * seed_count)
        )
        output.append(
            {
                "scenario_id": scenario_id,
                "row_count": row_count,
                "strategy_id": strategy_id,
                "seed_count": seed_count,
                "median_candidate_ms": statistics.median(
                    float(row["candidate_median_ms"]) for row in rows
                ),
                "median_fused_ms": statistics.median(float(row["fused_median_ms"]) for row in rows),
                "median_speedup_vs_fused_percent": median_speedup,
                "paired_ci95_lower_percent": lower,
                "paired_ci95_upper_percent": upper,
                "win_seed_count": win_count,
                "tie_seed_count": outcomes.count("tie"),
                "loss_seed_count": outcomes.count("loss"),
                "median_regret_vs_oracle_percent": statistics.median(
                    float(row["regret_vs_oracle_percent"]) for row in rows
                ),
                "stable_nonfused_reversal": stable,
            }
        )
    return output


def _scenario_rows(
    comparisons: list[dict[str, Any]],
    paired_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_scenario = _group_by(comparisons, ("scenario_id", "row_count"))
    paired_by_scenario = _group_by(paired_rows, ("scenario_id", "row_count"))
    output: list[dict[str, Any]] = []
    for key, rows in sorted(by_scenario.items()):
        scenario_id, row_count = key
        # Paired ratios, not two independently aggregated latency medians, are
        # the decision statistic. This avoids Simpson-style ranking reversals.
        best = max(rows, key=lambda row: float(row["median_speedup_vs_fused_percent"]))
        seed_rows = paired_by_scenario[key]
        oracle_by_seed: dict[int, float] = {}
        for row in seed_rows:
            seed = int(row["data_seed"])
            if bool(row["is_oracle_for_seed"]):
                oracle_by_seed[seed] = max(
                    oracle_by_seed.get(seed, float("-inf")),
                    float(row["speedup_vs_fused_percent"]),
                )
        output.append(
            {
                "scenario_id": scenario_id,
                "row_count": row_count,
                "seed_count": int(best["seed_count"]),
                "best_strategy_by_seed_median": best["strategy_id"],
                "best_median_latency_ms": best["median_candidate_ms"],
                "fused_median_latency_ms": best["median_fused_ms"],
                "best_speedup_vs_fused_percent": best["median_speedup_vs_fused_percent"],
                "oracle_median_speedup_vs_fused_percent": statistics.median(
                    oracle_by_seed.values()
                ),
                "stable_nonfused_reversal": bool(best["stable_nonfused_reversal"]),
            }
        )
    return output


def _markdown_report(summary: dict[str, Any], scenarios: list[dict[str, Any]]) -> str:
    lines = [
        "# Phase 2C paired analysis",
        "",
        "Repeated timings are summarized within each data seed before paired comparison.",
        "Positive speedup means the selected strategy is faster than `fused`.",
        "",
        (
            "| Scenario | Rows | Best seed-median strategy | Speedup vs fused | "
            "Oracle speedup | Stable reversal |"
        ),
        "|---|---:|---|---:|---:|---|",
    ]
    for row in scenarios:
        lines.append(
            "| {scenario_id} | {row_count} | {best_strategy_by_seed_median} | "
            "{best_speedup_vs_fused_percent:.2f}% | "
            "{oracle_median_speedup_vs_fused_percent:.2f}% | {stable} |".format(
                **row,
                stable="yes" if row["stable_nonfused_reversal"] else "no",
            )
        )
    lines.extend(
        [
            "",
            f"Stable non-fused reversals: **{summary['stable_nonfused_reversal_count']}**.",
            "",
            "This screening report does not turn an isolated per-seed win into a system claim.",
        ]
    )
    return "\n".join(lines) + "\n"


def analyze_phase2c_run(
    run_dir: str | Path,
    *,
    bootstrap_runs: int = 2000,
    bootstrap_seed: int = 20260715,
    minimum_stable_seeds: int = 5,
) -> Path:
    """Create deterministic derived reports without modifying raw artifacts."""

    if bootstrap_runs < 100:
        raise ValueError("bootstrap_runs must be at least 100")
    if minimum_stable_seeds < 2:
        raise ValueError("minimum_stable_seeds must be at least two")
    resolved = Path(run_dir).resolve()
    run_summary = _validate_complete_run(resolved)
    strategy_rows = _read_csv(resolved / "strategy_summary.csv")
    if not strategy_rows:
        raise ValueError("Phase 2C strategy_summary.csv is empty")
    tie_threshold = float(run_summary["tie_threshold_fraction"])
    paired = _paired_rows(strategy_rows, tie_threshold=tie_threshold)
    comparisons = _strategy_comparisons(
        paired,
        tie_threshold=tie_threshold,
        bootstrap_seed=bootstrap_seed,
        bootstrap_runs=bootstrap_runs,
        minimum_stable_seeds=minimum_stable_seeds,
    )
    scenarios = _scenario_rows(comparisons, paired)
    nonfused = [row for row in comparisons if row["strategy_id"] != "fused"]
    best_nonfused = max(
        nonfused,
        key=lambda row: float(row["median_speedup_vs_fused_percent"]),
        default=None,
    )
    summary = {
        "run_id": run_summary.get("run_id", resolved.name),
        "source_status": run_summary["status"],
        "source_results_equivalent": run_summary["all_results_equivalent"],
        "unit_count": int(run_summary["unit_count"]),
        "paired_unit_strategy_count": len(paired),
        "scenario_scale_count": len(scenarios),
        "stable_nonfused_reversal_count": sum(
            bool(row["stable_nonfused_reversal"]) for row in scenarios
        ),
        "best_nonfused_screening_result": best_nonfused,
        "tie_threshold_fraction": tie_threshold,
        "bootstrap_runs": bootstrap_runs,
        "bootstrap_seed": bootstrap_seed,
        "minimum_stable_seeds": minimum_stable_seeds,
        "note": (
            "Intervals bootstrap paired data-seed summaries. Five seeds remain a screening "
            "sample, so results require confirmation before a paper claim."
        ),
    }
    output_dir = resolved / "analysis"
    _write_csv(output_dir / "paired_unit_comparisons.csv", paired)
    _write_csv(output_dir / "paired_strategy_comparisons.csv", comparisons)
    _write_csv(output_dir / "scenario_summary.csv", scenarios)
    _write_json(output_dir / "analysis_summary.json", summary)
    (output_dir / "report.md").write_text(_markdown_report(summary, scenarios), encoding="utf-8")
    return output_dir
