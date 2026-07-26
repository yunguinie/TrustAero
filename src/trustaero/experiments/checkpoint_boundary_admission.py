"""Admission analysis for a policy-required checkpoint cost boundary.

The analysis asks whether policy selectivity and sensitive width add predictive
information beyond the strongest global query-selectivity threshold.  It does
not fit the final optimizer.  A complex model is rejected when one global
threshold already explains more than the predeclared fraction of conclusive
scenarios.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.reproducibility.source_freeze import sha256_file

POLICY_WIN = "policy_first_narrow_checkpoint"
QUERY_WIN = "query_first_raw_checkpoint"


@dataclass(frozen=True, slots=True)
class CheckpointBoundaryAdmissionConfig:
    """Predeclared comparison against the strongest global q threshold."""

    results_dir: str
    measurement_results_dir: str
    minimum_conclusive_scenario_rate: float
    minimum_query_levels_with_cross_factor_winner_interaction: int
    maximum_best_global_query_threshold_accuracy: float
    require_policy_first_singleton_winner: bool
    require_query_first_singleton_winner: bool
    require_clean_git: bool


def load_checkpoint_boundary_admission_config(
    path: Path | str,
) -> CheckpointBoundaryAdmissionConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return CheckpointBoundaryAdmissionConfig(
        results_dir=str(payload["results_dir"]),
        measurement_results_dir=str(payload["measurement_results_dir"]),
        minimum_conclusive_scenario_rate=float(payload["minimum_conclusive_scenario_rate"]),
        minimum_query_levels_with_cross_factor_winner_interaction=int(
            payload["minimum_query_levels_with_cross_factor_winner_interaction"]
        ),
        maximum_best_global_query_threshold_accuracy=float(
            payload["maximum_best_global_query_threshold_accuracy"]
        ),
        require_policy_first_singleton_winner=bool(
            payload["require_policy_first_singleton_winner"]
        ),
        require_query_first_singleton_winner=bool(payload["require_query_first_singleton_winner"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def _dimensions(scenario_id: str) -> tuple[int, int, float, float]:
    """Parse the frozen n-w-p-q scenario identifier."""

    parts = scenario_id.split("-")
    if len(parts) != 4:
        raise ValueError(f"Invalid checkpoint scenario ID: {scenario_id}")
    try:
        return (
            int(parts[0][1:]),
            int(parts[1][1:]),
            float(parts[2][1:]),
            float(parts[3][1:]),
        )
    except (ValueError, IndexError) as error:
        raise ValueError(f"Invalid checkpoint scenario ID: {scenario_id}") from error


def _winner(conclusion: str) -> str | None:
    if conclusion == "LEFT_MATERIALLY_FASTER":
        return POLICY_WIN
    if conclusion == "LEFT_MATERIALLY_SLOWER":
        return QUERY_WIN
    return None


def _best_global_threshold(
    rows: list[dict[str, object]],
) -> tuple[float, float]:
    """Find the best simple rule: query-first below q, policy-first otherwise."""

    query_values = sorted({cast(float, row["query_selectivity"]) for row in rows})
    if not query_values:
        return 0.0, 0.0
    candidates = [query_values[0] - 1e-9]
    candidates.extend(
        (left + right) / 2.0 for left, right in zip(query_values, query_values[1:], strict=False)
    )
    candidates.append(query_values[-1] + 1e-9)
    scored: list[tuple[float, float]] = []
    for threshold in candidates:
        correct = 0
        for row in rows:
            predicted = (
                QUERY_WIN if cast(float, row["query_selectivity"]) < threshold else POLICY_WIN
            )
            correct += predicted == row["winner"]
        scored.append((correct / len(rows), threshold))
    accuracy, threshold = max(scored, key=lambda item: (item[0], -item[1]))
    return threshold, accuracy


def analyze_checkpoint_boundary_admission(
    measurement_summary: dict[str, Any],
    config: CheckpointBoundaryAdmissionConfig,
) -> dict[str, object]:
    """Apply the frozen multi-factor-necessity gates."""

    rows: list[dict[str, object]] = []
    for scenario in cast(
        list[dict[str, Any]],
        measurement_summary["scenario_results"],
    ):
        winner = _winner(str(scenario["conclusion"]))
        row_count, width, policy, query = _dimensions(str(scenario["scenario_id"]))
        rows.append(
            {
                "scenario_id": str(scenario["scenario_id"]),
                "row_count": row_count,
                "identifier_width": width,
                "policy_selectivity": policy,
                "query_selectivity": query,
                "winner": winner,
            }
        )
    conclusive = [row for row in rows if row["winner"] is not None]
    conclusive_rate = len(conclusive) / len(rows) if rows else 0.0
    threshold, threshold_accuracy = _best_global_threshold(conclusive)

    winners_by_query: dict[float, set[str]] = defaultdict(set)
    for row in conclusive:
        winners_by_query[cast(float, row["query_selectivity"])].add(cast(str, row["winner"]))
    interaction_levels = sorted(
        query for query, winners in winners_by_query.items() if winners == {POLICY_WIN, QUERY_WIN}
    )
    winner_ids = {cast(str, row["winner"]) for row in conclusive}
    gates = {
        "measurement_integrity": (
            measurement_summary["status"] == "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY"
        ),
        "minimum_conclusive_scenario_rate": (
            conclusive_rate >= config.minimum_conclusive_scenario_rate
        ),
        "policy_first_singleton_winner": (
            POLICY_WIN in winner_ids or not config.require_policy_first_singleton_winner
        ),
        "query_first_singleton_winner": (
            QUERY_WIN in winner_ids or not config.require_query_first_singleton_winner
        ),
        "minimum_cross_factor_interaction_levels": (
            len(interaction_levels)
            >= config.minimum_query_levels_with_cross_factor_winner_interaction
        ),
        "global_threshold_not_sufficient": (
            threshold_accuracy <= config.maximum_best_global_query_threshold_accuracy
        ),
    }
    passed = all(gates.values())
    return {
        "schema_version": 1,
        "status": (
            "PASS_CHECKPOINT_MULTIFACTOR_OPTIMIZER_ADMISSION"
            if passed
            else "FAIL_CHECKPOINT_MULTIFACTOR_OPTIMIZER_ADMISSION_RETAIN"
        ),
        "optimizer_training_authorized": passed,
        "scenario_count": len(rows),
        "conclusive_scenario_count": len(conclusive),
        "conclusive_scenario_rate": conclusive_rate,
        "winner_counts": {
            candidate_id: sum(row["winner"] == candidate_id for row in conclusive)
            for candidate_id in (POLICY_WIN, QUERY_WIN)
        },
        "query_levels_with_cross_factor_winner_interaction": interaction_levels,
        "best_global_query_threshold": threshold,
        "best_global_query_threshold_accuracy": threshold_accuracy,
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "scientific_boundary": (
            "This development admission test compares label structure with a "
            "global q-threshold baseline. It does not evaluate a trained optimizer "
            "and cannot authorize a paper performance claim."
        ),
        "paper_optimizer_performance_claim_authorized": False,
    }


def run_checkpoint_boundary_admission(
    config: CheckpointBoundaryAdmissionConfig,
    *,
    project_root: Path,
    measurement_run_id: str,
    config_path: Path,
) -> Path:
    """Bind one completed measurement and persist the admission decision."""

    root = project_root.resolve()
    measurement_dir = root / config.measurement_results_dir / measurement_run_id
    summary_path = measurement_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Checkpoint boundary admission requires a clean worktree")
    result = analyze_checkpoint_boundary_admission(summary, config)
    result["measurement_run_id"] = measurement_run_id
    result["measurement_summary_sha256"] = sha256_file(summary_path)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "config.json", asdict(config))
    _atomic_json(
        output_dir / "environment.json",
        {
            "commit_hash": commit,
            "git_dirty": dirty,
            "config_sha256": sha256_file(config_path),
        },
    )
    _atomic_json(output_dir / "evaluation.json", result)
    _atomic_json(output_dir.parent / "latest_run.json", {"run_id": run_id})
    return output_dir
