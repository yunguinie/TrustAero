"""Formal development admission for the Experiment 1 multi-Join family."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.execution_aware_oracle_stability import (
    classify_ratio_interval,
    confidence_undominated_set,
)
from trustaero.experiments.execution_flow_inference import hierarchical_paired_log_ratio_ci
from trustaero.experiments.tpch_audit import verify_tpch_artifact

GOVERNED_FACT_FIRST = "governed_fact_first"
ELIGIBLE_DIMENSION_FIRST = "eligible_dimension_first"
PARTIAL_AGGREGATE_FIRST = "partial_aggregate_first"
CANDIDATE_IDS = (
    GOVERNED_FACT_FIRST,
    ELIGIBLE_DIMENSION_FIRST,
    PARTIAL_AGGREGATE_FIRST,
)


@dataclass(frozen=True, slots=True)
class WorkloadUnit:
    policy_selectivity: float
    query_selectivity: float
    salt: int

    @property
    def policy_cutoff(self) -> int:
        return round(self.policy_selectivity * 10_000)

    @property
    def query_cutoff(self) -> int:
        return round(self.query_selectivity * 10_000)

    @property
    def scenario_id(self) -> str:
        return f"p{self.policy_selectivity:.3f}-q{self.query_selectivity:.3f}"

    @property
    def unit_id(self) -> str:
        return f"{self.scenario_id}-s{self.salt}"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _digest(rows: list[tuple[Any, ...]]) -> str:
    return hashlib.sha256(json.dumps(rows, default=str, separators=(",", ":")).encode()).hexdigest()


def _governed_projection(unit: WorkloadUnit) -> str:
    return f"""
        SELECT l_orderkey,
               l_linenumber,
               (l_extendedprice * (1 - l_discount))::DECIMAL(38, 4) AS revenue,
               md5(l_comment) AS masked_token
        FROM lineitem
        WHERE hash(l_comment, {unit.salt}) % 10000 < {unit.policy_cutoff}
    """


def _customer_predicate(unit: WorkloadUnit, alias: str = "c") -> str:
    return f"hash({alias}.c_comment, {unit.salt + 65537}) % 10000 < {unit.query_cutoff}"


def candidate_sql(candidate_id: str, unit: WorkloadUnit) -> tuple[tuple[str, ...], str]:
    """Compile three legal, semantically equivalent physical realizations."""

    governed = _governed_projection(unit)
    if candidate_id == GOVERNED_FACT_FIRST:
        return (
            (f"CREATE TEMP TABLE governed_lineitem AS {governed}",),
            f"""
                SELECT c.c_mktsegment,
                       count(*)::HUGEINT AS governed_count,
                       sum(g.revenue)::DECIMAL(38, 4) AS governed_revenue,
                       min(g.masked_token) AS minimum_masked_token
                FROM governed_lineitem AS g
                INNER JOIN orders AS o ON g.l_orderkey = o.o_orderkey
                INNER JOIN customer AS c ON o.o_custkey = c.c_custkey
                WHERE {_customer_predicate(unit)}
                GROUP BY c.c_mktsegment
                ORDER BY c.c_mktsegment
            """,
        )
    if candidate_id == ELIGIBLE_DIMENSION_FIRST:
        return (
            (
                f"""
                    CREATE TEMP TABLE eligible_orders AS
                    SELECT o.o_orderkey, c.c_mktsegment
                    FROM orders AS o
                    INNER JOIN customer AS c ON o.o_custkey = c.c_custkey
                    WHERE {_customer_predicate(unit)}
                """,
            ),
            f"""
                WITH governed_lineitem AS MATERIALIZED ({governed})
                SELECT e.c_mktsegment,
                       count(*)::HUGEINT AS governed_count,
                       sum(g.revenue)::DECIMAL(38, 4) AS governed_revenue,
                       min(g.masked_token) AS minimum_masked_token
                FROM governed_lineitem AS g
                INNER JOIN eligible_orders AS e ON g.l_orderkey = e.o_orderkey
                GROUP BY e.c_mktsegment
                ORDER BY e.c_mktsegment
            """,
        )
    if candidate_id == PARTIAL_AGGREGATE_FIRST:
        return (
            (
                f"""
                    CREATE TEMP TABLE governed_by_order AS
                    SELECT l_orderkey,
                           count(*)::HUGEINT AS governed_count,
                           sum(revenue)::DECIMAL(38, 4) AS governed_revenue,
                           min(masked_token) AS minimum_masked_token
                    FROM ({governed}) AS governed
                    GROUP BY l_orderkey
                """,
            ),
            f"""
                SELECT c.c_mktsegment,
                       sum(g.governed_count)::HUGEINT AS governed_count,
                       sum(g.governed_revenue)::DECIMAL(38, 4) AS governed_revenue,
                       min(g.minimum_masked_token) AS minimum_masked_token
                FROM governed_by_order AS g
                INNER JOIN orders AS o ON g.l_orderkey = o.o_orderkey
                INNER JOIN customer AS c ON o.o_custkey = c.c_custkey
                WHERE {_customer_predicate(unit)}
                GROUP BY c.c_mktsegment
                ORDER BY c.c_mktsegment
            """,
        )
    raise ValueError(f"Unknown Experiment 1 candidate: {candidate_id}")


def _drop(connection: Any) -> None:
    for table in (
        "governed_output",
        "governed_lineitem",
        "eligible_orders",
        "governed_by_order",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _execute(
    connection: Any,
    candidate_id: str,
    unit: WorkloadUnit,
    *,
    capture_lineage: bool = False,
) -> tuple[float, str, str | None]:
    _drop(connection)
    setup, output = candidate_sql(candidate_id, unit)
    started = time.perf_counter_ns()
    for statement in setup:
        connection.execute(statement)
    connection.execute(f"CREATE TEMP TABLE governed_output AS {output}")
    latency_ms = (time.perf_counter_ns() - started) / 1_000_000
    rows = connection.execute("SELECT * FROM governed_output ORDER BY c_mktsegment").fetchall()
    lineage_digest = None
    if capture_lineage:
        lineage = connection.execute(
            f"""
                SELECT c.c_mktsegment, l.l_orderkey, l.l_linenumber
                FROM lineitem AS l
                INNER JOIN orders AS o ON l.l_orderkey = o.o_orderkey
                INNER JOIN customer AS c ON o.o_custkey = c.c_custkey
                WHERE hash(l.l_comment, {unit.salt}) % 10000 < {unit.policy_cutoff}
                  AND {_customer_predicate(unit)}
                ORDER BY c.c_mktsegment, l.l_orderkey, l.l_linenumber
            """
        ).fetchall()
        lineage_digest = _digest(lineage)
    result_digest = _digest(rows)
    _drop(connection)
    return latency_ms, result_digest, lineage_digest


def _fingerprint(connection: Any, candidate_id: str, unit: WorkloadUnit) -> str:
    _drop(connection)
    setup, output = candidate_sql(candidate_id, unit)
    fingerprints: list[str] = []
    for statement in setup:
        fingerprints.append(observe_duckdb_plan(connection, statement, analyze=False).fingerprint)
        connection.execute(statement)
    fingerprints.append(observe_duckdb_plan(connection, output, analyze=False).fingerprint)
    _drop(connection)
    return hashlib.sha256("|".join(fingerprints).encode()).hexdigest()


def _orders(repetitions: int, seed: int) -> tuple[tuple[str, ...], ...]:
    values = list(itertools.permutations(CANDIDATE_IDS)) * repetitions
    random.Random(seed).shuffle(values)
    return tuple(values)


def _stable_seed(base: int, label: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{base}:{label}".encode()).digest()[:8], "big")


def _analyze(rows: list[dict[str, Any]], protocol: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["scenario_id"])].append(row)
    scenario_results: list[dict[str, Any]] = []
    singleton_winners: list[str] = []
    for scenario_id, scenario_rows in sorted(grouped.items()):
        blocks: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in scenario_rows:
            blocks[(int(row["salt"]), int(row["block_index"]))][str(row["candidate_id"])] = float(
                row["latency_ms"]
            )
        if any(set(values) != set(CANDIDATE_IDS) for values in blocks.values()):
            raise RuntimeError(f"Incomplete paired block: {scenario_id}")
        pairwise: list[dict[str, Any]] = []
        for left, right in itertools.combinations(CANDIDATE_IDS, 2):
            ratios: dict[int, list[float]] = defaultdict(list)
            for (salt, _), values in sorted(blocks.items()):
                ratios[salt].append(math.log(values[left] / values[right]))
            point, lower, upper = hierarchical_paired_log_ratio_ci(
                ratios,
                confidence_level=float(protocol["confidence_level"]),
                repetitions=int(protocol["bootstrap_draws"]),
                seed=_stable_seed(int(protocol["bootstrap_seed"]), f"{scenario_id}:{left}:{right}"),
            )
            pairwise.append(
                {
                    "left_candidate_id": left,
                    "right_candidate_id": right,
                    "left_over_right_ratio": point,
                    "confidence_interval": [lower, upper],
                    "conclusion": classify_ratio_interval(
                        lower, upper, float(protocol["practical_tie_fraction"])
                    ),
                }
            )
        confidence_set = confidence_undominated_set(CANDIDATE_IDS, pairwise)
        winner = confidence_set[0] if len(confidence_set) == 1 else None
        if winner is not None:
            singleton_winners.append(winner)
        medians = {
            candidate: statistics.median(
                float(row["latency_ms"])
                for row in scenario_rows
                if row["candidate_id"] == candidate
            )
            for candidate in CANDIDATE_IDS
        }
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "candidate_median_ms": medians,
                "confidence_undominated_candidate_ids": list(confidence_set),
                "singleton_winner": winner,
                "pairwise": pairwise,
            }
        )
    counts = Counter(singleton_winners)
    conclusive_rate = len(singleton_winners) / max(len(scenario_results), 1)
    gates = {
        "minimum_conclusive_scenario_rate": conclusive_rate
        >= float(protocol["admission_gates"]["minimum_conclusive_scenario_rate"]),
        "minimum_distinct_singleton_winners": len(counts)
        >= int(protocol["admission_gates"]["minimum_distinct_singleton_winners"]),
    }
    return {
        "status": (
            "PASS_EXPERIMENT1_DEVELOPMENT_ADMISSION"
            if all(gates.values())
            else "STOP_EXPERIMENT1_DEVELOPMENT_ADMISSION"
        ),
        "gates": gates,
        "conclusive_scenario_rate": conclusive_rate,
        "singleton_winner_counts": dict(sorted(counts.items())),
        "scenario_results": scenario_results,
    }


def run_development(protocol_path: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    protocol = _load(protocol_path)
    if protocol.get("status") != "FROZEN_DEVELOPMENT_BEFORE_TIMING":
        raise ValueError("Experiment 1 development protocol is not frozen")
    if tuple(protocol["candidate_ids"]) != CANDIDATE_IDS:
        raise ValueError("Experiment 1 candidate set changed")
    database, artifact = verify_tpch_artifact(root, scale_factor=int(protocol["scale_factor"]))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / str(protocol["results_dir"]) / run_id
    output.mkdir(parents=True, exist_ok=False)
    _atomic_json(output / "protocol_snapshot.json", protocol)
    units = tuple(
        WorkloadUnit(float(item["policy_selectivity"]), float(item["query_selectivity"]), int(salt))
        for item in protocol["scenarios"]
        for salt in protocol["salts"]
    )
    rows: list[dict[str, Any]] = []
    preflight: list[dict[str, Any]] = []
    import duckdb

    for index, unit in enumerate(units, start=1):
        connection = duckdb.connect(str(database), read_only=True)
        try:
            connection.execute(f"SET threads = {int(protocol['duckdb_threads'])}")
            connection.execute(f"SET memory_limit = '{int(protocol['duckdb_memory_limit_mb'])}MB'")
            semantic = {
                candidate: _execute(connection, candidate, unit, capture_lineage=True)
                for candidate in CANDIDATE_IDS
            }
            if len({value[1] for value in semantic.values()}) != 1:
                raise RuntimeError(f"Result mismatch: {unit.unit_id}")
            if len({value[2] for value in semantic.values()}) != 1:
                raise RuntimeError(f"Lineage mismatch: {unit.unit_id}")
            fingerprints = {
                candidate: _fingerprint(connection, candidate, unit) for candidate in CANDIDATE_IDS
            }
            if len(set(fingerprints.values())) != len(CANDIDATE_IDS):
                raise RuntimeError(f"Physical plans collapsed: {unit.unit_id}")
            preflight.append(
                {
                    "unit_id": unit.unit_id,
                    "result_digest": semantic[CANDIDATE_IDS[0]][1],
                    "lineage_digest": semantic[CANDIDATE_IDS[0]][2],
                    "physical_fingerprints": fingerprints,
                }
            )
            seed = _stable_seed(int(protocol["order_seed"]), unit.unit_id)
            for order in _orders(int(protocol["warmup_rounds_per_permutation"]), seed):
                for candidate in order:
                    _execute(connection, candidate, unit)
            for block_index, order in enumerate(
                _orders(int(protocol["measured_rounds_per_permutation"]), seed + 1)
            ):
                for position, candidate in enumerate(order):
                    latency, digest, _ = _execute(connection, candidate, unit)
                    if digest != semantic[CANDIDATE_IDS[0]][1]:
                        raise RuntimeError(f"Timed result mismatch: {unit.unit_id}")
                    rows.append(
                        {
                            "scenario_id": unit.scenario_id,
                            "unit_id": unit.unit_id,
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
        print(f"[Experiment 1 dev {index}/{len(units)}] {unit.unit_id}", flush=True)
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    analysis = _analyze(rows, protocol)
    summary = {
        **analysis,
        "protocol_id": protocol["protocol_id"],
        "run_id": run_id,
        "scale_factor": protocol["scale_factor"],
        "artifact_sha256": artifact["sha256"],
        "scenario_count": len(protocol["scenarios"]),
        "experimental_unit_count": len(units),
        "measurement_count": len(rows),
        "preflight": preflight,
        "claim_boundary": protocol["claim_boundary"],
    }
    _atomic_json(output / "summary.json", summary)
    _atomic_json(
        root / str(protocol["results_dir"]) / "latest_run.json",
        {"run_id": run_id, "status": summary["status"]},
    )
    return output
