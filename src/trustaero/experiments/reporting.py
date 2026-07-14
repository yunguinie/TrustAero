"""Summarize Phase 0 experiment outputs across one or more runs."""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict
from pathlib import Path

from trustaero.experiments.models import Phase0RunSummary


def _bool(value: str) -> bool:
    """Parse bools written by ``csv.DictWriter`` from dataclass values."""

    return value == "True"


def _float(value: str) -> float:
    """Parse an optional float cell from cases.csv."""

    return float(value) if value else 0.0


def _read_cases(path: Path) -> tuple[dict[str, str], ...]:
    """Read one Phase 0 cases.csv as raw string rows."""

    with path.open(newline="", encoding="utf-8") as handle:
        return tuple(csv.DictReader(handle))


def summarize_run(run_dir: Path) -> Phase0RunSummary:
    """Summarize one ``results/phase0/<run_id>`` directory."""

    rows = _read_cases(run_dir / "cases.csv")
    run_id = run_dir.name
    commit_hash = rows[0].get("commit_hash", "unknown") if rows else "unknown"
    case_count = len(rows)
    status_correct = sum(_bool(row["status_correct"]) for row in rows)
    reason_correct = sum(_bool(row["reason_code_correct"]) for row in rows)

    # Detection rate focuses on injected or negative cases. A case is treated
    # as a violation if it expects REJECT or has a non-baseline scenario.
    violation_rows = [
        row
        for row in rows
        if row["expected_status"] == "REJECT" or row.get("scenario", "baseline") != "baseline"
    ]
    detected = [
        row
        for row in violation_rows
        if row["actual_status"] == row["expected_status"] and _bool(row["reason_code_correct"])
    ]

    # False reject rate focuses on cases expected to pass validation or return
    # the current certificate PARTIAL status.
    legal_rows = [row for row in rows if row["expected_status"] in {"ACCEPT", "REWRITE", "PARTIAL"}]
    false_rejects = [row for row in legal_rows if row["actual_status"] == "REJECT"]

    medians = [_float(row["median_latency_ms"]) for row in rows]
    p95s = [_float(row["p95_latency_ms"]) for row in rows]
    maxes = [_float(row["max_latency_ms"]) for row in rows]
    failed_cases = tuple(
        row["case_id"]
        for row in rows
        if not _bool(row["status_correct"]) or not _bool(row["reason_code_correct"])
    )

    return Phase0RunSummary(
        run_id=run_id,
        commit_hash=commit_hash,
        case_count=case_count,
        status_accuracy=status_correct / case_count if case_count else 0.0,
        reason_code_accuracy=reason_correct / case_count if case_count else 0.0,
        detection_rate=len(detected) / len(violation_rows) if violation_rows else 1.0,
        false_reject_rate=len(false_rejects) / len(legal_rows) if legal_rows else 0.0,
        median_latency_ms=statistics.median(medians) if medians else 0.0,
        p95_latency_ms=max(p95s) if p95s else 0.0,
        max_latency_ms=max(maxes) if maxes else 0.0,
        all_correct=not failed_cases,
        failed_cases=failed_cases,
    )


def summarize_phase0(results_dir: Path, output_dir: Path) -> tuple[Phase0RunSummary, ...]:
    """Summarize every Phase 0 run directory under ``results_dir``."""

    run_dirs = sorted(
        path for path in results_dir.iterdir() if path.is_dir() and (path / "cases.csv").exists()
    )
    summaries = tuple(summarize_run(path) for path in run_dirs)
    output_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = list(Phase0RunSummary.__annotations__)
    with (output_dir / "phase0_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for summary in summaries:
            row = asdict(summary)
            row["failed_cases"] = "|".join(summary.failed_cases)
            writer.writerow(row)

    (output_dir / "phase0_summary.json").write_text(
        json.dumps(
            [asdict(summary) for summary in summaries],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summaries
