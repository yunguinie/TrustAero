"""Experiment 2: hard legality-first pruning versus soft penalties."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.experiments.governed_pipeline_cost_calibration import EQUIVALENCE_GROUP
from trustaero.experiments.real_governed_pipeline_transfer import _load_real_observations

Key = tuple[str, int, str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def select_soft_penalty(
    predicted_latency_ms: dict[str, float],
    legal_candidate_ids: tuple[str, ...],
    penalty_ms: float,
) -> str:
    """Rank every candidate after adding one penalty per infeasible plan."""

    if penalty_ms < 0.0:
        raise ValueError("Penalty must be nonnegative")
    legal = set(legal_candidate_ids)
    if not predicted_latency_ms or not legal:
        raise ValueError("Predictions and legal candidates must be nonempty")
    return min(
        predicted_latency_ms,
        key=lambda candidate: (
            predicted_latency_ms[candidate] + penalty_ms * (candidate not in legal),
            candidate,
        ),
    )


def minimum_strict_penalty_ms(
    predicted_latency_ms: dict[str, float],
    legal_candidate_ids: tuple[str, ...],
) -> float:
    """Return the infimum penalty above which an illegal minimum cannot win."""

    legal = set(legal_candidate_ids)
    illegal = set(predicted_latency_ms) - legal
    if not illegal:
        return 0.0
    best_legal = min(predicted_latency_ms[candidate] for candidate in legal)
    best_illegal = min(predicted_latency_ms[candidate] for candidate in illegal)
    return max(0.0, best_legal - best_illegal)


def _nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def evaluate_selection(
    actual_latency_ms: dict[Key, dict[str, float]],
    legal_by_key: dict[Key, tuple[str, ...]],
    selected_by_key: dict[Key, str],
    practical_tie_fraction: float,
) -> dict[str, Any]:
    """Evaluate legality and quality against the per-unit legal Oracle."""

    illegal_count = 0
    within_count = 0
    legal_regrets: list[float] = []
    decisions: list[dict[str, Any]] = []
    for key in sorted(actual_latency_ms):
        actual = actual_latency_ms[key]
        legal = legal_by_key[key]
        selected = selected_by_key[key]
        if selected not in actual:
            raise ValueError(f"Selected candidate lacks timing: {key}={selected}")
        best = min(actual[candidate] for candidate in legal)
        oracle = tuple(
            sorted(
                candidate
                for candidate in legal
                if actual[candidate] <= best * (1.0 + practical_tie_fraction)
            )
        )
        is_legal = selected in legal
        illegal_count += not is_legal
        oracle_hit = is_legal and selected in oracle
        within_count += oracle_hit
        regret = None
        if is_legal:
            regret = (actual[selected] / best - 1.0) * 100.0
            legal_regrets.append(regret)
        decisions.append(
            {
                "scenario_id": key[0],
                "seed": key[1],
                "selected_candidate_id": selected,
                "legal_candidate_ids": list(legal),
                "legal_oracle_candidate_ids": list(oracle),
                "is_legal": is_legal,
                "within_3_percent": oracle_hit,
                "legal_selection_regret_percent": regret,
            }
        )
    count = len(decisions)
    return {
        "decision_count": count,
        "illegal_selection_count": illegal_count,
        "illegal_selection_rate": illegal_count / count,
        "within_3_percent_count": within_count,
        "within_3_percent_rate": within_count / count,
        "legal_selection_count": len(legal_regrets),
        "conditional_mean_regret_percent": (
            statistics.mean(legal_regrets) if legal_regrets else None
        ),
        "conditional_p95_regret_percent": _nearest_rank(legal_regrets, 0.95),
        "conditional_max_regret_percent": max(legal_regrets) if legal_regrets else None,
        "selected_candidate_counts": dict(sorted(Counter(selected_by_key.values()).items())),
        "decisions": decisions,
    }


def _decision_map(items: list[dict[str, Any]]) -> dict[Key, dict[str, Any]]:
    result: dict[Key, dict[str, Any]] = {}
    for item in items:
        key = (str(item["scenario_id"]), int(item["seed"]), EQUIVALENCE_GROUP)
        if key in result:
            raise ValueError(f"Duplicate decision: {key}")
        result[key] = item
    return result


def run_experiment(protocol_path: Path, project_root: Path) -> Path:
    """Run the frozen offline comparison once over the consumed real holdout."""

    root = project_root.resolve()
    protocol = _load(protocol_path)
    if protocol.get("status") != "FROZEN_BEFORE_EXPERIMENT2_RUN":
        raise ValueError("Experiment 2 protocol is not frozen")
    for binding in protocol["immutable_inputs"]:
        path = root / str(binding["path"])
        if _sha256(path) != str(binding["sha256"]):
            raise ValueError(f"Experiment 2 input changed: {path}")

    evaluation = _load(root / str(protocol["source_evaluation"]))
    observations, _, integrity = _load_real_observations(
        root / str(protocol["source_measurement_run"])
    )
    actual: dict[Key, dict[str, float]] = defaultdict(dict)
    for item in observations:
        key = (item.scenario_id, item.seed, item.equivalence_group)
        actual[key][item.candidate_id] = float(item.latency_ms)

    permissive = _decision_map(evaluation["regime_results"]["permissive"]["decisions"])
    predictions = {
        key: {
            str(candidate): float(value)
            for candidate, value in item["predicted_latency_ms"].items()
        }
        for key, item in permissive.items()
    }
    if set(actual) != set(predictions):
        raise ValueError("Experiment 2 timing and prediction units differ")

    regime_results: dict[str, Any] = {}
    for regime_id in protocol["policy_regimes"]:
        source = _decision_map(evaluation["regime_results"][regime_id]["decisions"])
        legal = {
            key: tuple(str(value) for value in item["legal_candidate_ids"])
            for key, item in source.items()
        }
        hard_selected = {key: str(item["selected_candidate_id"]) for key, item in source.items()}
        methods: dict[str, Any] = {
            "hard_legality_first": evaluate_selection(
                actual,
                legal,
                hard_selected,
                float(protocol["practical_tie_fraction"]),
            )
        }
        for penalty in protocol["penalty_grid_ms"]:
            value = float(penalty)
            selected = {
                key: select_soft_penalty(predictions[key], legal[key], value) for key in actual
            }
            methods[f"soft_penalty_{value:g}ms"] = evaluate_selection(
                actual,
                legal,
                selected,
                float(protocol["practical_tie_fraction"]),
            )
        thresholds = [minimum_strict_penalty_ms(predictions[key], legal[key]) for key in actual]
        by_dataset: dict[str, list[float]] = defaultdict(list)
        for key, threshold in zip(sorted(actual), thresholds, strict=True):
            dataset = "bts" if key[0].startswith("bts-") else "nyc"
            by_dataset[dataset].append(threshold)
        zero_illegal_grid = [
            float(penalty)
            for penalty in protocol["penalty_grid_ms"]
            if methods[f"soft_penalty_{float(penalty):g}ms"]["illegal_selection_count"] == 0
        ]
        regime_results[regime_id] = {
            "methods": methods,
            "penalty_threshold_ms": {
                "minimum": min(thresholds),
                "median": statistics.median(thresholds),
                "p95": _nearest_rank(thresholds, 0.95),
                "maximum": max(thresholds),
                "by_dataset": {
                    dataset: {
                        "minimum": min(values),
                        "median": statistics.median(values),
                        "maximum": max(values),
                    }
                    for dataset, values in sorted(by_dataset.items())
                },
                "smallest_scanned_zero_illegal_penalty": (
                    min(zero_illegal_grid) if zero_illegal_grid else None
                ),
            },
        }

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / str(protocol["results_dir"]) / run_id
    summary = {
        "schema_version": 1,
        "status": "COMPLETE_EXPERIMENT2_HARD_VS_SOFT",
        "protocol_id": protocol["protocol_id"],
        "run_id": run_id,
        "analysis_boundary": (
            "Offline replay over the consumed frozen policy-stratified holdout; "
            "no model fitting, timing rerun, or new holdout claim."
        ),
        "violation_score": "binary: 0 for legal and 1 for any infeasible candidate",
        "score": "predicted_latency_ms + penalty_ms * violation_score",
        "integrity": integrity,
        "regime_results": regime_results,
    }
    _atomic_json(output / "protocol_snapshot.json", protocol)
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / str(protocol["results_dir"]) / "latest_run.json",
        {"run_id": run_id, "status": summary["status"]},
    )
    return output
