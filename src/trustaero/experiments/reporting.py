"""Summarize Phase 0 experiment outputs across one or more runs."""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from trustaero.experiments.models import (
    Phase0CategorySummary,
    Phase0ReasonCodeSummary,
    Phase0RunSummary,
)

SummaryRow = Phase0RunSummary | Phase0CategorySummary | Phase0ReasonCodeSummary


def _bool(value: str) -> bool:
    """Parse bools written by ``csv.DictWriter`` from dataclass values."""

    return value == "True"


def _float(value: str) -> float:
    """Parse an optional float cell from cases.csv."""

    return float(value) if value else 0.0


def _codes(value: str) -> tuple[str, ...]:
    """Split the pipe-delimited reason code cells written by the runner."""

    return tuple(code for code in value.split("|") if code)


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


def summarize_categories(run_dir: Path) -> tuple[Phase0CategorySummary, ...]:
    """Group one run by case category for paper-facing coverage tables."""

    rows = _read_cases(run_dir / "cases.csv")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["case_category"]].append(row)

    summaries: list[Phase0CategorySummary] = []
    for category in sorted(grouped):
        category_rows = grouped[category]
        medians = [_float(row["median_latency_ms"]) for row in category_rows]
        p95s = [_float(row["p95_latency_ms"]) for row in category_rows]
        failed_cases = tuple(
            row["case_id"]
            for row in category_rows
            if not _bool(row["status_correct"]) or not _bool(row["reason_code_correct"])
        )
        summaries.append(
            Phase0CategorySummary(
                run_id=run_dir.name,
                case_category=category,
                case_count=len(category_rows),
                status_correct=sum(_bool(row["status_correct"]) for row in category_rows),
                reason_code_correct=sum(_bool(row["reason_code_correct"]) for row in category_rows),
                median_latency_ms=statistics.median(medians) if medians else 0.0,
                p95_latency_ms=max(p95s) if p95s else 0.0,
                failed_cases=failed_cases,
            )
        )
    return tuple(summaries)


def summarize_reason_codes(run_dir: Path) -> tuple[Phase0ReasonCodeSummary, ...]:
    """Count expected and observed diagnostics without flattening to one code.

    A Phase 0 case may expect multiple reason codes. Keeping them separate
    makes the output useful for later fault-injection experiments, where the
    question is not just "did it reject?" but also "which violation was caught?".
    """

    rows = _read_cases(run_dir / "cases.csv")
    expected_counts: dict[str, int] = defaultdict(int)
    actual_counts: dict[str, int] = defaultdict(int)
    matched_counts: dict[str, int] = defaultdict(int)

    for row in rows:
        expected = set(_codes(row["expected_reason_codes"]))
        actual = set(_codes(row["actual_reason_codes"]))
        for code in expected:
            expected_counts[code] += 1
        for code in actual:
            actual_counts[code] += 1
        for code in expected & actual:
            matched_counts[code] += 1

    reason_codes = sorted(set(expected_counts) | set(actual_counts))
    return tuple(
        Phase0ReasonCodeSummary(
            run_id=run_dir.name,
            reason_code=code,
            expected_count=expected_counts[code],
            actual_count=actual_counts[code],
            matched_count=matched_counts[code],
        )
        for code in reason_codes
    )


def _write_dataclass_csv(path: Path, rows: Iterable[SummaryRow], fieldnames: list[str]) -> None:
    """Write dataclass rows to CSV, normalizing tuple fields for readability."""

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            row = asdict(item)
            for key, value in row.items():
                if isinstance(value, tuple):
                    row[key] = "|".join(value)
            writer.writerow(row)


def _write_dataclass_json(path: Path, rows: Iterable[SummaryRow]) -> None:
    """Write dataclass rows to stable JSON for reproducible experiment artifacts."""

    path.write_text(
        json.dumps([asdict(item) for item in rows], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def summarize_phase0(results_dir: Path, output_dir: Path) -> tuple[Phase0RunSummary, ...]:
    """Summarize every Phase 0 run directory under ``results_dir``."""

    run_dirs = sorted(
        path for path in results_dir.iterdir() if path.is_dir() and (path / "cases.csv").exists()
    )
    summaries = tuple(summarize_run(path) for path in run_dirs)
    category_summaries = tuple(
        summary for path in run_dirs for summary in summarize_categories(path)
    )
    reason_code_summaries = tuple(
        summary for path in run_dirs for summary in summarize_reason_codes(path)
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_dataclass_csv(
        output_dir / "phase0_summary.csv",
        summaries,
        list(Phase0RunSummary.__annotations__),
    )
    _write_dataclass_json(output_dir / "phase0_summary.json", summaries)
    _write_dataclass_csv(
        output_dir / "phase0_category_summary.csv",
        category_summaries,
        list(Phase0CategorySummary.__annotations__),
    )
    _write_dataclass_json(output_dir / "phase0_category_summary.json", category_summaries)
    _write_dataclass_csv(
        output_dir / "phase0_reason_code_summary.csv",
        reason_code_summaries,
        list(Phase0ReasonCodeSummary.__annotations__),
    )
    _write_dataclass_json(
        output_dir / "phase0_reason_code_summary.json",
        reason_code_summaries,
    )
    return summaries
