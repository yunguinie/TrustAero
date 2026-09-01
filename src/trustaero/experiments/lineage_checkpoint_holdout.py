"""Evaluate the frozen Lineage checkpoint model on one untouched holdout."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_aware_calibration import (
    NonnegativeAnalyticFit,
    evaluate_fit_selections,
)
from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.experiments.lineage_checkpoint_calibration import (
    EQUIVALENCE_GROUP,
    STABLE_PREFERENCES,
    BaselineDecision,
    _decision,
    _groups,
    _metrics,
    _scenario_query_counts,
    evaluate_fixed_baselines,
    load_lineage_calibration_observations,
)
from trustaero.optimizer.lineage_checkpoint_space import (
    LATE_PER_QUERY_CAPTURE,
    POLICY_LINEAGE_CHECKPOINT,
    SNAPSHOT_LINEAGE_CHECKPOINT,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model(path: Path) -> tuple[NonnegativeAnalyticFit, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    calibration = payload["calibration"]
    root = Path(__file__).resolve().parents[3]
    calibration_path = root / str(calibration["path"])
    if _sha256(calibration_path) != calibration["sha256"]:
        raise ValueError("Frozen Lineage model calibration digest changed")
    model = payload["model"]
    return (
        NonnegativeAnalyticFit(
            intercept_ms=float(model["intercept_ms"]),
            coefficients=tuple(
                sorted((str(name), float(value)) for name, value in model["coefficients"].items())
            ),
            ridge_lambda=float(model["ridge_lambda"]),
            iterations=0,
            converged=True,
        ),
        payload,
    )


def _selection_metrics(decisions: tuple[Any, ...]) -> dict[str, Any]:
    regrets = sorted(float(item.regret_percent) for item in decisions)
    p95 = regrets[min(len(regrets) - 1, math.ceil(0.95 * len(regrets)) - 1)]
    return {
        "decision_count": len(decisions),
        "oracle_set_hit_rate": statistics.mean(item.oracle_hit for item in decisions),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": p95,
        "maximum_regret_percent": max(regrets),
        "decisions": [asdict(item) for item in decisions],
    }


def _threshold_baseline(
    observations: tuple[Any, ...],
    *,
    query_counts: dict[str, int],
    lower: int,
    upper: int,
    practical_tie_fraction: float,
) -> dict[str, Any]:
    decisions: list[BaselineDecision] = []
    for (scenario_id, _seed), candidates in _groups(observations).items():
        query_count = query_counts[scenario_id]
        if query_count <= lower:
            selected = LATE_PER_QUERY_CAPTURE
        elif query_count <= upper:
            selected = POLICY_LINEAGE_CHECKPOINT
        else:
            selected = SNAPSHOT_LINEAGE_CHECKPOINT
        decisions.append(_decision(candidates, selected, practical_tie_fraction))
    return _metrics(decisions)


def evaluate_lineage_checkpoint_holdout(
    run_dir: Path,
    *,
    model_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Evaluate without fitting or changing any frozen parameter."""

    run_dir = run_dir.resolve()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    complete_statuses = {
        "PASS_LINEAGE_CHECKPOINT_OPTIMIZER_ADMISSION",
        "FAIL_LINEAGE_CHECKPOINT_OPTIMIZER_ADMISSION_RETAIN",
    }
    if summary.get("status") not in complete_statuses:
        raise ValueError("Lineage holdout measurement is incomplete")
    observations = load_lineage_calibration_observations(
        run_dir,
        allow_complete_admission_negative=True,
    )
    fit, frozen = _model(model_path.resolve())
    tie = float(frozen["model"]["practical_tie_fraction"])
    decisions = evaluate_fit_selections(
        fit,
        observations,
        stable_preferences=STABLE_PREFERENCES,
        practical_tie_fraction=tie,
    )
    model_metrics = _selection_metrics(decisions)
    fixed = evaluate_fixed_baselines(observations, practical_tie_fraction=tie)
    best_fixed_id, best_fixed = min(
        fixed.items(),
        key=lambda item: (item[1]["mean_regret_percent"], item[0]),
    )
    threshold_spec = frozen["strong_threshold_baseline"]
    threshold = _threshold_baseline(
        observations,
        query_counts=_scenario_query_counts(run_dir),
        lower=int(threshold_spec["lower_query_count_threshold"]),
        upper=int(threshold_spec["upper_query_count_threshold"]),
        practical_tie_fraction=tie,
    )
    gates = {
        "minimum_oracle_set_hit_rate": model_metrics["oracle_set_hit_rate"] >= 0.8,
        "maximum_mean_regret": model_metrics["mean_regret_percent"] <= 3.0,
        "maximum_p95_regret": model_metrics["p95_regret_percent"] <= 10.0,
        "maximum_regret": model_metrics["maximum_regret_percent"] <= 20.0,
        "beats_best_fixed_mean": (
            model_metrics["mean_regret_percent"] < best_fixed["mean_regret_percent"]
        ),
        "beats_best_fixed_p95": (
            model_metrics["p95_regret_percent"] < best_fixed["p95_regret_percent"]
        ),
        "not_worse_than_threshold_hit": (
            model_metrics["oracle_set_hit_rate"] >= threshold["oracle_set_hit_rate"]
        ),
        "not_worse_than_threshold_mean": (
            model_metrics["mean_regret_percent"] <= threshold["mean_regret_percent"] + 1e-12
        ),
    }
    passed = all(gates.values())
    payload = {
        "status": (
            "PASS_LINEAGE_CHECKPOINT_COST_MODEL_INDEPENDENT_HOLDOUT"
            if passed
            else "FAIL_LINEAGE_CHECKPOINT_COST_MODEL_HOLDOUT_RETAIN"
        ),
        "source_run": run_dir.as_posix(),
        "frozen_model_path": model_path.resolve().as_posix(),
        "equivalence_group": EQUIVALENCE_GROUP,
        "model": model_metrics,
        "fixed_baselines": fixed,
        "best_fixed_candidate_id": best_fixed_id,
        "threshold_baseline": threshold,
        "gates": gates,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "evaluation.json", payload)
    return payload
