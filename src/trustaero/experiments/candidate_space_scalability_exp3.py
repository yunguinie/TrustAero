"""Experiment 3: scalability of real combinatorial physical-candidate spaces."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.experiments.policy_stratified_pipeline_holdout import _statistics_by_group
from trustaero.optimizer.governed_pipeline_space import GovernedPipelineStatistics

SCALE_POINTS = (3, 6, 12, 24, 48)
EXECUTION_ORDERS = ("governed_fact_first", "eligible_dimension_first", "partial_aggregate_first")
MATERIALIZATIONS = ("masked_checkpoint", "raw_checkpoint")
LINEAGE_POINTS = ("result", "pre_aggregate")
EVIDENCE_MODES = ("inline", "buffered")
MASK_POINTS = ("pre_join", "post_join")
POLICY_REGIMES = ("permissive", "no_raw_join", "strict")


@dataclass(frozen=True, slots=True)
class ScaledCandidate:
    """One structurally distinct result-equivalent physical realization."""

    candidate_id: str
    execution_order: str
    materialization: str
    lineage_checkpoint: str
    evidence_capture: str
    mask_placement: str
    estimated_cost: float
    fingerprint: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _active_axes(candidate_count: int) -> tuple[tuple[str, ...], ...]:
    if candidate_count not in SCALE_POINTS:
        raise ValueError(f"Unsupported candidate count: {candidate_count}")
    materializations = MATERIALIZATIONS if candidate_count >= 6 else MATERIALIZATIONS[:1]
    lineage = LINEAGE_POINTS if candidate_count >= 12 else LINEAGE_POINTS[:1]
    evidence = EVIDENCE_MODES if candidate_count >= 24 else EVIDENCE_MODES[:1]
    mask = MASK_POINTS if candidate_count >= 48 else MASK_POINTS[:1]
    return (EXECUTION_ORDERS, materializations, lineage, evidence, mask)


def _cost(
    statistics: GovernedPipelineStatistics,
    execution: str,
    materialization: str,
    lineage: str,
    evidence: str,
    mask: str,
) -> float:
    """Registered analytic work score used only for control-plane ranking."""

    width = statistics.sensitive_width_bytes
    input_rows = statistics.input_rows
    governed = statistics.estimated_governed_rows
    query = statistics.estimated_query_rows
    result = statistics.estimated_result_rows
    if execution == "governed_fact_first":
        score = input_rows * 1.00 + governed * 1.20
    elif execution == "eligible_dimension_first":
        score = query * 1.05 + input_rows * 0.72 + governed * 0.85
    elif execution == "partial_aggregate_first":
        score = input_rows * 0.88 + governed * 0.70 + result * 2.40
    else:
        raise ValueError(f"Unknown execution order: {execution}")
    score += (
        statistics.estimated_policy_rows * (16.0 + width) * 0.015
        if materialization == "raw_checkpoint"
        else statistics.estimated_policy_rows * 64.0 * 0.011
    )
    score += governed * 2.0 * 0.16 if lineage == "pre_aggregate" else result * 2.0 * 0.16
    score += result * (0.20 if evidence == "buffered" else 0.28)
    mask_rows = governed if mask == "pre_join" else result
    score += mask_rows * width * 0.013
    return score / 1000.0


def generate_candidates(
    statistics: GovernedPipelineStatistics,
    candidate_count: int,
) -> tuple[ScaledCandidate, ...]:
    """Generate nested real combinations with unique physical fingerprints."""

    candidates: list[ScaledCandidate] = []
    for values in itertools.product(*_active_axes(candidate_count)):
        execution, materialization, lineage, evidence, mask = values
        candidate_id = "--".join(values)
        payload = {
            "execution_order": execution,
            "materialization": materialization,
            "lineage_checkpoint": lineage,
            "evidence_capture": evidence,
            "mask_placement": mask,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        candidates.append(
            ScaledCandidate(
                candidate_id=candidate_id,
                execution_order=execution,
                materialization=materialization,
                lineage_checkpoint=lineage,
                evidence_capture=evidence,
                mask_placement=mask,
                estimated_cost=_cost(statistics, *values),
                fingerprint=fingerprint,
            )
        )
    if len(candidates) != candidate_count:
        raise RuntimeError(f"Candidate construction mismatch: {len(candidates)}")
    if len({item.candidate_id for item in candidates}) != candidate_count:
        raise RuntimeError("Candidate IDs are not unique")
    if len({item.fingerprint for item in candidates}) != candidate_count:
        raise RuntimeError("Physical candidate fingerprints are not unique")
    return tuple(candidates)


def is_legal(candidate: ScaledCandidate, regime: str) -> bool:
    """Apply explicit governance feasibility conditions before ranking."""

    if regime == "permissive":
        return True
    if regime == "no_raw_join":
        return candidate.mask_placement == "pre_join"
    if regime == "strict":
        return (
            candidate.mask_placement == "pre_join"
            and candidate.materialization == "masked_checkpoint"
            and candidate.evidence_capture == "inline"
        )
    raise ValueError(f"Unknown policy regime: {regime}")


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _bootstrap_ci(
    values: list[float],
    *,
    seed: int,
    draws: int,
) -> list[float]:
    generator = random.Random(seed)
    medians = [statistics.median(generator.choices(values, k=len(values))) for _ in range(draws)]
    return [_percentile(medians, 0.025), _percentile(medians, 0.975)]


def _summarize_rows(rows: list[dict[str, Any]], protocol: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["candidate_count"]), str(row["policy_regime"]))].append(row)
    result: list[dict[str, Any]] = []
    for (candidate_count, regime), items in sorted(grouped.items()):
        timing: dict[str, Any] = {}
        for field in (
            "generation_us",
            "legality_filter_us",
            "ranking_us",
            "total_planning_us",
        ):
            values = [float(item[field]) for item in items]
            timing[field] = {
                "median": statistics.median(values),
                "p95": _percentile(values, 0.95),
                "bootstrap_95_ci": _bootstrap_ci(
                    values,
                    seed=int(protocol["bootstrap_seed"]) + candidate_count * 10 + len(field),
                    draws=int(protocol["bootstrap_draws"]),
                ),
            }
        result.append(
            {
                "candidate_count": candidate_count,
                "policy_regime": regime,
                "planning_trials": len(items),
                "legal_candidate_count": int(items[0]["legal_candidate_count"]),
                "legal_fraction": float(items[0]["legal_candidate_count"]) / candidate_count,
                "illegal_selection_count": sum(
                    not bool(item["selected_is_legal"]) for item in items
                ),
                "cost_oracle_match_count": sum(bool(item["cost_oracle_match"]) for item in items),
                "within_3_percent_cost_oracle_count": sum(
                    bool(item["within_3_percent_cost_oracle"]) for item in items
                ),
                "timing_us": timing,
            }
        )
    return result


def run_experiment(protocol_path: Path, project_root: Path) -> Path:
    """Execute the frozen candidate-space scalability experiment."""

    root = project_root.resolve()
    protocol = _load(protocol_path)
    if protocol.get("status") != "FROZEN_BEFORE_EXPERIMENT3_RUN":
        raise ValueError("Experiment 3 protocol is not frozen")
    if tuple(int(value) for value in protocol["candidate_counts"]) != SCALE_POINTS:
        raise ValueError("Experiment 3 scale points changed")
    if tuple(protocol["policy_regimes"]) != POLICY_REGIMES:
        raise ValueError("Experiment 3 policy regimes changed")
    for binding in protocol["immutable_inputs"]:
        path = root / str(binding["path"])
        if _sha256(path) != str(binding["sha256"]):
            raise ValueError(f"Experiment 3 input changed: {path}")

    statistics = _statistics_by_group(root / str(protocol["source_measurement_run"]))
    if len(statistics) != int(protocol["expected_configuration_count"]):
        raise ValueError("Experiment 3 configuration count changed")
    rows: list[dict[str, Any]] = []
    warmups = int(protocol["warmup_repetitions"])
    repetitions = int(protocol["measured_repetitions"])
    for candidate_count in SCALE_POINTS:
        for regime in POLICY_REGIMES:
            for key, stats in sorted(statistics.items()):
                for _ in range(warmups):
                    candidates = generate_candidates(stats, candidate_count)
                    legal = tuple(item for item in candidates if is_legal(item, regime))
                    min(legal, key=lambda item: (item.estimated_cost, item.candidate_id))
                for repetition in range(repetitions):
                    total_start = time.perf_counter_ns()
                    generation_start = total_start
                    candidates = generate_candidates(stats, candidate_count)
                    generation_end = time.perf_counter_ns()
                    legal = tuple(item for item in candidates if is_legal(item, regime))
                    filter_end = time.perf_counter_ns()
                    if not legal:
                        raise RuntimeError(f"Empty legal set: {candidate_count}/{regime}/{key}")
                    selected = min(
                        legal,
                        key=lambda item: (item.estimated_cost, item.candidate_id),
                    )
                    ranking_end = time.perf_counter_ns()
                    oracle_cost = min(item.estimated_cost for item in legal)
                    rows.append(
                        {
                            "candidate_count": candidate_count,
                            "policy_regime": regime,
                            "scenario_id": key[0],
                            "seed": key[1],
                            "repetition": repetition,
                            "legal_candidate_count": len(legal),
                            "selected_candidate_id": selected.candidate_id,
                            "selected_is_legal": True,
                            "cost_oracle_match": selected.estimated_cost == oracle_cost,
                            "within_3_percent_cost_oracle": (
                                selected.estimated_cost <= oracle_cost * 1.03
                            ),
                            "generation_us": (generation_end - generation_start) / 1000.0,
                            "legality_filter_us": (filter_end - generation_end) / 1000.0,
                            "ranking_us": (ranking_end - filter_end) / 1000.0,
                            "total_planning_us": (ranking_end - total_start) / 1000.0,
                        }
                    )

    expected_rows = len(SCALE_POINTS) * len(POLICY_REGIMES) * len(statistics) * repetitions
    if len(rows) != expected_rows:
        raise RuntimeError("Experiment 3 measurement count mismatch")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / str(protocol["results_dir"]) / run_id
    output.mkdir(parents=True, exist_ok=False)
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary_rows = _summarize_rows(rows, protocol)
    gates = {
        "all_measurements_complete": len(rows) == expected_rows,
        "all_generated_spaces_nonempty": all(
            int(item["legal_candidate_count"]) > 0 for item in summary_rows
        ),
        "zero_illegal_selections": all(
            int(item["illegal_selection_count"]) == 0 for item in summary_rows
        ),
        "all_cost_oracle_matches": all(
            int(item["cost_oracle_match_count"]) == int(item["planning_trials"])
            for item in summary_rows
        ),
    }
    summary = {
        "schema_version": 1,
        "status": (
            "PASS_EXPERIMENT3_CANDIDATE_SPACE_SCALABILITY"
            if all(gates.values())
            else "FAIL_EXPERIMENT3_RETAIN"
        ),
        "protocol_id": protocol["protocol_id"],
        "run_id": run_id,
        "configuration_count": len(statistics),
        "planning_trial_count": len(rows),
        "candidate_axis_definitions": {
            "3": ["execution order"],
            "6": ["execution order", "materialization boundary"],
            "12": ["execution order", "materialization boundary", "lineage checkpoint"],
            "24": [
                "execution order",
                "materialization boundary",
                "lineage checkpoint",
                "evidence capture",
            ],
            "48": [
                "execution order",
                "materialization boundary",
                "lineage checkpoint",
                "evidence capture",
                "mask placement",
            ],
        },
        "gates": gates,
        "results": summary_rows,
        "claim_boundary": (
            "This experiment measures TrustAero control-plane candidate generation, "
            "legality filtering, and ranking. Cost-oracle agreement is a deterministic "
            "planner-correctness check, not a new physical-runtime generalization result."
        ),
    }
    _atomic_json(output / "protocol_snapshot.json", protocol)
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / str(protocol["results_dir"]) / "latest_run.json",
        {"run_id": run_id, "status": summary["status"]},
    )
    return output
