"""Repeatable Phase 0 validator micro-experiment runner."""

from __future__ import annotations

import csv
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.experiments.loader import load_cases, load_catalog, load_json, load_policy
from trustaero.experiments.models import CaseResult, ExperimentCase, Phase0Config
from trustaero.validator.service import validate


def _repo_root() -> Path:
    """Return the repository root from this source file location."""

    return Path(__file__).resolve().parents[3]


def _git_commit(root: Path) -> str:
    """Record the exact source revision when git metadata is available."""

    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip()


def _run_id() -> str:
    """Use a sortable UTC run ID so result folders are easy to compare."""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _percentile_95(values: list[float]) -> float:
    """Return a simple nearest-rank P95 for short repeated measurements."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def _reason_codes(response: Any) -> tuple[str, ...]:
    """Extract sorted stable reason-code strings from a validator response."""

    return tuple(sorted({diagnostic.code.value for diagnostic in response.diagnostics}))


def _operator_counts(plan: dict[str, Any]) -> tuple[int, int]:
    """Return logical operator and edge counts from a raw candidate plan."""

    operators = plan.get("operators", [])
    if not isinstance(operators, list):
        return 0, 0
    edge_count = 0
    for operator in operators:
        if isinstance(operator, dict) and isinstance(operator.get("inputs"), list):
            edge_count += len(operator["inputs"])
    return len(operators), edge_count


def _case_result(
    *,
    root: Path,
    run_id: str,
    commit_hash: str,
    case: ExperimentCase,
    warmup_runs: int,
    measured_runs: int,
) -> CaseResult:
    """Run one case with cold and preloaded core-validation measurements."""

    plan_path = root / case.plan_path
    policy_path = root / case.policy_path
    catalog_path = root / case.catalog_path

    # Cold timing includes file reads and typed policy/catalog loading. It is
    # useful for artifact users, but it is not the clean validator micro-cost.
    cold_start = time.perf_counter()
    raw_plan = load_json(plan_path)
    policy = load_policy(policy_path)
    catalog = load_catalog(catalog_path)
    cold_response = validate(raw_plan, policy, catalog)
    cold_latency_ms = (time.perf_counter() - cold_start) * 1000.0

    for _ in range(warmup_runs):
        validate(raw_plan, policy, catalog)

    measurements: list[float] = []
    for _ in range(measured_runs):
        started = time.perf_counter()
        validate(raw_plan, policy, catalog)
        measurements.append((time.perf_counter() - started) * 1000.0)

    actual_reason_codes = _reason_codes(cold_response)
    expected_reason_codes = tuple(sorted(case.expected_reason_codes))
    status_correct = cold_response.status.value == case.expected_status
    reason_code_correct = set(expected_reason_codes).issubset(set(actual_reason_codes))
    if not expected_reason_codes:
        reason_code_correct = not actual_reason_codes

    operator_count, edge_count = _operator_counts(raw_plan)
    validated_plan = cold_response.validated_plan
    rewrite_rounds = None
    inserted_operator_count = 0
    pending_obligation_count = 0
    verified_obligation_count = 0
    plan_digest = ""
    if validated_plan is not None:
        rewrite_rounds = validated_plan.validation.rounds
        inserted_operator_count = max(0, len(validated_plan.operators) - operator_count)
        pending_obligation_count = len(validated_plan.pending_obligations)
        verified_obligation_count = len(validated_plan.satisfied_obligations)
        plan_digest = validated_plan.validation.canonical_digest

    return CaseResult(
        run_id=run_id,
        commit_hash=commit_hash,
        case_id=case.case_id,
        case_category=case.case_category,
        expected_status=case.expected_status,
        actual_status=cold_response.status.value,
        status_correct=status_correct,
        expected_reason_codes=expected_reason_codes,
        actual_reason_codes=actual_reason_codes,
        reason_code_correct=reason_code_correct,
        runs=measured_runs,
        warmup_runs=warmup_runs,
        cold_latency_ms=cold_latency_ms,
        median_latency_ms=statistics.median(measurements),
        p95_latency_ms=_percentile_95(measurements),
        min_latency_ms=min(measurements),
        max_latency_ms=max(measurements),
        plan_size_bytes=plan_path.stat().st_size,
        operator_count=operator_count,
        edge_count=edge_count,
        rewrite_rounds=rewrite_rounds,
        inserted_operator_count=inserted_operator_count,
        pending_obligation_count=pending_obligation_count,
        verified_obligation_count=verified_obligation_count,
        certificate_event_count=0,
        plan_digest=plan_digest,
    )


def _write_cases_csv(path: Path, results: tuple[CaseResult, ...]) -> None:
    """Write stable-column per-case results."""

    fieldnames = list(asdict(results[0]).keys()) if results else list(CaseResult.__annotations__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["expected_reason_codes"] = "|".join(result.expected_reason_codes)
            row["actual_reason_codes"] = "|".join(result.actual_reason_codes)
            writer.writerow(row)


def _environment(root: Path, commit_hash: str) -> dict[str, object]:
    """Capture enough environment data to interpret Phase 0 numbers."""

    try:
        trustaero_version = metadata.version("trustaero")
    except metadata.PackageNotFoundError:
        trustaero_version = "editable-or-uninstalled"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "trustaero_version": trustaero_version,
        "commit_hash": commit_hash,
        "repo_root": str(root),
    }


def _summary(results: tuple[CaseResult, ...]) -> dict[str, object]:
    """Aggregate correctness and latency summaries for quick inspection."""

    total = len(results)
    status_correct = sum(result.status_correct for result in results)
    reason_correct = sum(result.reason_code_correct for result in results)
    failed = [
        result.case_id
        for result in results
        if not result.status_correct or not result.reason_code_correct
    ]
    medians = [result.median_latency_ms for result in results]
    return {
        "case_count": total,
        "status_correct": status_correct,
        "reason_code_correct": reason_correct,
        "all_correct": not failed,
        "failed_cases": failed,
        "median_of_case_medians_ms": statistics.median(medians) if medians else 0.0,
        "max_case_p95_ms": max((result.p95_latency_ms for result in results), default=0.0),
    }


def run_phase0(config: Phase0Config) -> Path:
    """Run Phase 0 and return the created result directory."""

    root = _repo_root()
    run_id = _run_id()
    commit_hash = _git_commit(root)
    cases = load_cases(root / config.cases_path)
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=False)

    results = tuple(
        _case_result(
            root=root,
            run_id=run_id,
            commit_hash=commit_hash,
            case=case,
            warmup_runs=config.warmup_runs,
            measured_runs=config.measured_runs,
        )
        for case in cases
    )

    _write_cases_csv(output_dir / "cases.csv", results)
    (output_dir / "summary.json").write_text(
        json.dumps(_summary(results), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "environment.json").write_text(
        json.dumps(_environment(root, commit_hash), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "config.json").write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "failures").mkdir(exist_ok=True)
    return output_dir
