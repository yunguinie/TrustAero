"""One-shot SF10 scale-and-configuration holdout for Experiment 1."""

from __future__ import annotations

import csv
import hashlib
import json
import random
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.experiments.tpch_audit import verify_tpch_artifact
from trustaero.experiments.tpch_multijoin_aggregate_exp1 import (
    CANDIDATE_IDS,
    ELIGIBLE_DIMENSION_FIRST,
    GOVERNED_FACT_FIRST,
    PARTIAL_AGGREGATE_FIRST,
    WorkloadUnit,
    _analyze,
    _atomic_json,
    _execute,
    _fingerprint,
    _orders,
    _stable_seed,
)


def select_candidate(policy_selectivity: float, query_selectivity: float) -> str:
    """Frozen mechanism-aware selector learned from the complete SF1 grid."""

    if policy_selectivity <= 0.01:
        if query_selectivity >= 0.5:
            return PARTIAL_AGGREGATE_FIRST
        return GOVERNED_FACT_FIRST
    if policy_selectivity <= 0.1 and query_selectivity > 0.25:
        return GOVERNED_FACT_FIRST
    return ELIGIBLE_DIMENSION_FIRST


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


def _nearest_rank(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(quantile * len(ordered) + 0.999999) - 1))
    return ordered[index]


def _unit_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        units[str(row["unit_id"])].append(row)
    decisions: list[dict[str, Any]] = []
    for unit_id, unit_rows in sorted(units.items()):
        medians = {
            candidate: statistics.median(
                float(row["latency_ms"]) for row in unit_rows if row["candidate_id"] == candidate
            )
            for candidate in CANDIDATE_IDS
        }
        best = min(medians.values())
        first = unit_rows[0]
        selected = select_candidate(
            float(first["policy_selectivity"]), float(first["query_selectivity"])
        )
        regret = (medians[selected] / best - 1.0) * 100.0
        decisions.append(
            {
                "scenario_id": first["scenario_id"],
                "unit_id": unit_id,
                "salt": int(first["salt"]),
                "selected_candidate_id": selected,
                "candidate_median_ms": medians,
                "legal_oracle_candidate_ids": sorted(
                    candidate for candidate, latency in medians.items() if latency <= best * 1.03
                ),
                "regret_percent": regret,
                "within_3_percent": regret <= 3.0,
            }
        )
    return decisions


def run_holdout(protocol_path: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    protocol = _load(protocol_path)
    if protocol.get("status") != "FROZEN_BEFORE_SF10_OPEN":
        raise ValueError("Experiment 1 holdout protocol is not frozen")
    for item in protocol["immutable_inputs"]:
        path = root / str(item["path"])
        if _sha256(path) != str(item["sha256"]):
            raise ValueError(f"Frozen Experiment 1 input changed: {path}")
    if tuple(protocol["candidate_ids"]) != CANDIDATE_IDS:
        raise ValueError("Experiment 1 holdout candidate set changed")
    database, artifact = verify_tpch_artifact(root, scale_factor=10)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / str(protocol["results_dir"]) / run_id
    output.mkdir(parents=True, exist_ok=False)
    _atomic_json(output / "protocol_snapshot.json", protocol)
    units = tuple(
        WorkloadUnit(float(item["policy_selectivity"]), float(item["query_selectivity"]), int(salt))
        for item in protocol["scenarios"]
        for salt in protocol["salts"]
    )
    import duckdb

    rows: list[dict[str, Any]] = []
    preflight: list[dict[str, Any]] = []
    for index, unit in enumerate(units, start=1):
        connection = duckdb.connect(str(database), read_only=True)
        try:
            connection.execute(f"SET threads = {int(protocol['timing']['duckdb_threads'])}")
            connection.execute(
                f"SET memory_limit = '{int(protocol['timing']['duckdb_memory_limit_mb'])}MB'"
            )
            semantic = {
                candidate: _execute(connection, candidate, unit) for candidate in CANDIDATE_IDS
            }
            if len({value[1] for value in semantic.values()}) != 1:
                raise RuntimeError(f"SF10 result mismatch: {unit.unit_id}")
            fingerprints = {
                candidate: _fingerprint(connection, candidate, unit) for candidate in CANDIDATE_IDS
            }
            if len(set(fingerprints.values())) != len(CANDIDATE_IDS):
                raise RuntimeError(f"SF10 physical plans collapsed: {unit.unit_id}")
            preflight.append(
                {
                    "unit_id": unit.unit_id,
                    "result_digest": semantic[CANDIDATE_IDS[0]][1],
                    "physical_fingerprints": fingerprints,
                }
            )
            warmup = list(CANDIDATE_IDS)
            warmup_seed = _stable_seed(int(protocol["timing"]["order_seed"]), unit.unit_id)
            random.Random(warmup_seed).shuffle(warmup)
            for candidate in warmup:
                _execute(connection, candidate, unit)
            orders = _orders(
                int(protocol["timing"]["measured_permutation_cycles"]),
                _stable_seed(int(protocol["timing"]["order_seed"]) + 1, unit.unit_id),
            )
            for block_index, order in enumerate(orders):
                for position, candidate in enumerate(order):
                    latency, digest, _ = _execute(connection, candidate, unit)
                    if digest != semantic[CANDIDATE_IDS[0]][1]:
                        raise RuntimeError(f"SF10 timed result mismatch: {unit.unit_id}")
                    rows.append(
                        {
                            "scenario_id": unit.scenario_id,
                            "unit_id": unit.unit_id,
                            "policy_selectivity": unit.policy_selectivity,
                            "query_selectivity": unit.query_selectivity,
                            "salt": unit.salt,
                            "candidate_id": candidate,
                            "block_index": block_index,
                            "order_position": position,
                            "permutation_id": "->".join(order),
                            "latency_ms": latency,
                            "result_digest": digest,
                        }
                    )
        finally:
            connection.close()
        print(f"[Experiment 1 SF10 {index}/{len(units)}] {unit.unit_id}", flush=True)
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    analysis_protocol = {
        **protocol["inference"],
        "admission_gates": {
            "minimum_conclusive_scenario_rate": 0.0,
            "minimum_distinct_singleton_winners": 0,
        },
    }
    scenario_analysis = _analyze(rows, analysis_protocol)
    decisions = _unit_metrics(rows)
    regrets = [float(item["regret_percent"]) for item in decisions]
    expected_measurements = len(units) * 6 * len(CANDIDATE_IDS)
    gates = {
        "all_units_complete": len(preflight) == len(units),
        "all_measurements_complete": len(rows) == expected_measurements,
        "all_results_equivalent": len(preflight) == len(units),
        "all_physical_plans_distinct": len(preflight) == len(units),
        "no_illegal_selection": all(
            item["selected_candidate_id"] in CANDIDATE_IDS for item in decisions
        ),
    }
    summary = {
        "schema_version": 1,
        "status": "PASS_EXPERIMENT1_SF10_HOLDOUT_COMPLETE" if all(gates.values()) else "FAIL",
        "protocol_id": protocol["protocol_id"],
        "run_id": run_id,
        "artifact_sha256": artifact["sha256"],
        "scale_factor": 10,
        "configuration_count": len(protocol["scenarios"]),
        "planning_decision_count": len(decisions),
        "measurement_count": len(rows),
        "gates": gates,
        "planner_quality": {
            "within_3_percent_count": sum(bool(item["within_3_percent"]) for item in decisions),
            "within_3_percent_rate": sum(bool(item["within_3_percent"]) for item in decisions)
            / len(decisions),
            "mean_regret_percent": statistics.mean(regrets),
            "p95_regret_percent": _nearest_rank(regrets, 0.95),
            "max_regret_percent": max(regrets),
        },
        "selector": protocol["selector"],
        "decisions": decisions,
        "scenario_results": scenario_analysis["scenario_results"],
        "preflight": preflight,
        "claim_boundary": protocol["claim_boundary"],
    }
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / str(protocol["results_dir"]) / "latest_run.json",
        {"run_id": run_id, "status": summary["status"]},
    )
    return output
