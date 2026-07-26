"""Admission audit for a structurally distinct governed Join-Aggregate family.

Both candidates first create the same sanitized governance checkpoint.  They
then differ only in aggregate placement:

* ``join_then_aggregate`` joins governed rows and aggregates afterwards;
* ``partial_aggregate_then_join`` aggregates by the Join key before joining.

The runner is an admission test, not publication evidence.  It continues only
if both legal plans become statistically supported winners in different
scenarios while returning identical results and lineage.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import os
import platform
import random
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.execution_aware_oracle_stability import (
    classify_ratio_interval,
)
from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _git_state,
)
from trustaero.experiments.execution_flow_inference import (
    hierarchical_paired_log_ratio_ci,
)
from trustaero.optimizer.governed_aggregate_space import (
    AGGREGATE_CANDIDATE_IDS,
    JOIN_THEN_AGGREGATE,
    PARTIAL_AGGREGATE_THEN_JOIN,
    GovernedAggregateStatistics,
    plan_governed_aggregate,
)


@dataclass(frozen=True, slots=True)
class GovernedAggregateAdmissionConfig:
    """Frozen workload grid, paired timing protocol, and stop/go gates."""

    results_dir: str
    row_count: int
    identifier_width: int
    policy_selectivities: tuple[float, ...]
    query_selectivities: tuple[float, ...]
    key_domain_fractions: tuple[float, ...]
    seeds: tuple[int, ...]
    candidate_ids: tuple[str, ...]
    warmup_rounds_per_permutation: int
    measured_rounds_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    minimum_conclusive_scenario_rate: float
    require_clean_git: bool

    def __post_init__(self) -> None:
        if self.row_count <= 0 or self.identifier_width <= 0:
            raise ValueError("Aggregate admission sizes must be positive")
        if self.candidate_ids != AGGREGATE_CANDIDATE_IDS:
            raise ValueError("Aggregate admission candidate set changed")
        dimensions = (
            self.policy_selectivities,
            self.query_selectivities,
            self.key_domain_fractions,
            self.seeds,
        )
        if any(not values or len(values) != len(set(values)) for values in dimensions):
            raise ValueError("Aggregate admission dimensions must be nonempty and unique")
        if len(self.seeds) < 3:
            raise ValueError("Aggregate admission requires at least three seeds")
        if any(
            not 0.0 < value < 1.0 for value in self.policy_selectivities + self.query_selectivities
        ):
            raise ValueError("Selectivities must be in (0, 1)")
        if any(not 0.0 < value <= 1.0 for value in self.key_domain_fractions):
            raise ValueError("Key-domain fractions must be in (0, 1]")
        if self.warmup_rounds_per_permutation < 1:
            raise ValueError("Aggregate admission requires balanced warmup")
        if self.measured_rounds_per_permutation < 5:
            raise ValueError("Aggregate admission requires five measured rounds")
        if self.bootstrap_draws < 1_000:
            raise ValueError("Aggregate admission requires at least 1000 bootstrap draws")

    @property
    def blocks_per_unit(self) -> int:
        """Return paired blocks, not individual candidate executions."""

        return math.factorial(len(self.candidate_ids)) * self.measured_rounds_per_permutation


@dataclass(frozen=True, slots=True)
class GovernedAggregateUnit:
    """One indivisible policy-query-cardinality workload and random seed."""

    row_count: int
    identifier_width: int
    policy_selectivity: float
    query_selectivity: float
    key_domain_fraction: float
    seed: int

    @property
    def policy_cutoff(self) -> int:
        return round(self.policy_selectivity * 10_000)

    @property
    def query_cutoff(self) -> int:
        return round(self.query_selectivity * 10_000)

    @property
    def key_domain(self) -> int:
        return max(1, round(self.row_count * self.key_domain_fraction))

    @property
    def scenario_id(self) -> str:
        return (
            f"n{self.row_count}-p{self.policy_selectivity}-"
            f"q{self.query_selectivity}-k{self.key_domain_fraction}"
        )

    @property
    def unit_id(self) -> str:
        return f"{self.scenario_id}-s{self.seed}"


def load_governed_aggregate_admission_config(
    path: Path | str,
) -> GovernedAggregateAdmissionConfig:
    """Load a frozen aggregate-admission configuration."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return GovernedAggregateAdmissionConfig(
        results_dir=str(payload["results_dir"]),
        row_count=int(payload["row_count"]),
        identifier_width=int(payload["identifier_width"]),
        policy_selectivities=tuple(float(value) for value in payload["policy_selectivities"]),
        query_selectivities=tuple(float(value) for value in payload["query_selectivities"]),
        key_domain_fractions=tuple(float(value) for value in payload["key_domain_fractions"]),
        seeds=tuple(int(value) for value in payload["seeds"]),
        candidate_ids=tuple(str(value) for value in payload["candidate_ids"]),
        warmup_rounds_per_permutation=int(payload["warmup_rounds_per_permutation"]),
        measured_rounds_per_permutation=int(payload["measured_rounds_per_permutation"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_draws=int(payload["bootstrap_draws"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        minimum_conclusive_scenario_rate=float(payload["minimum_conclusive_scenario_rate"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def governed_aggregate_units(
    config: GovernedAggregateAdmissionConfig,
) -> tuple[GovernedAggregateUnit, ...]:
    """Expand the frozen grid without random row-level splitting."""

    return tuple(
        GovernedAggregateUnit(
            config.row_count,
            config.identifier_width,
            policy,
            query,
            key_fraction,
            seed,
        )
        for policy in config.policy_selectivities
        for query in config.query_selectivities
        for key_fraction in config.key_domain_fractions
        for seed in config.seeds
    )


def aggregate_candidate_sql(
    candidate_id: str,
    unit: GovernedAggregateUnit,
) -> tuple[tuple[str, ...], str]:
    """Compile a legal candidate after the common masked checkpoint."""

    checkpoint = f"""
        CREATE TEMP TABLE governed_checkpoint AS
        SELECT row_id, join_key, md5(sensitive_value) AS masked_value
        FROM events
        WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
          AND query_bucket < {unit.query_cutoff}
    """
    if candidate_id == JOIN_THEN_AGGREGATE:
        return (
            (checkpoint,),
            """
                SELECT dimension.marker, count(*)::HUGEINT AS governed_count
                FROM governed_checkpoint AS governed
                INNER JOIN dimension
                  ON governed.join_key = dimension.dimension_key
                GROUP BY dimension.marker
                ORDER BY dimension.marker
            """,
        )
    if candidate_id == PARTIAL_AGGREGATE_THEN_JOIN:
        return (
            (
                checkpoint,
                """
                    CREATE TEMP TABLE partial_counts AS
                    SELECT join_key, count(*)::HUGEINT AS partial_count
                    FROM governed_checkpoint
                    GROUP BY join_key
                """,
            ),
            """
                SELECT dimension.marker,
                       sum(partial.partial_count)::HUGEINT AS governed_count
                FROM partial_counts AS partial
                INNER JOIN dimension
                  ON partial.join_key = dimension.dimension_key
                GROUP BY dimension.marker
                ORDER BY dimension.marker
            """,
        )
    raise ValueError(f"Unknown governed aggregate candidate: {candidate_id}")


def _drop_temporary_tables(connection: Any) -> None:
    for table in ("governed_output", "partial_counts", "governed_checkpoint"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _create_data(connection: Any, unit: GovernedAggregateUnit) -> dict[str, int]:
    """Create deterministic rows with controlled group cardinality."""

    connection.execute("DROP TABLE IF EXISTS events")
    connection.execute("DROP TABLE IF EXISTS dimension")
    blocks = math.ceil(unit.identifier_width / 32)
    connection.execute(
        f"""
        CREATE TABLE events AS
        SELECT i::BIGINT AS row_id,
               (i % {unit.key_domain})::BIGINT AS join_key,
               left(repeat(md5(CAST(i + {unit.seed * 1_000_003} AS VARCHAR)),
                           {blocks}), {unit.identifier_width}) AS sensitive_value,
               (hash(i + {unit.seed * 97_003}) % 10000)::INTEGER AS query_bucket
        FROM range({unit.row_count}) AS source(i)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE dimension AS
        SELECT i::BIGINT AS dimension_key, (i % 97)::BIGINT AS marker
        FROM range({unit.key_domain}) AS source(i)
        """
    )
    row = connection.execute(
        f"""
        SELECT count(*), count(DISTINCT join_key)
        FROM events
        WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
          AND query_bucket < {unit.query_cutoff}
        """
    ).fetchone()
    if row is None:
        raise ValueError(f"Aggregate cardinality query failed: {unit.unit_id}")
    return {"governed_rows": int(row[0]), "governed_keys": int(row[1])}


def _run_candidate(
    connection: Any,
    candidate_id: str,
    unit: GovernedAggregateUnit,
    *,
    capture_lineage: bool = False,
) -> tuple[float, str, str | None]:
    """Time database work, then optionally derive lineage outside timing."""

    _drop_temporary_tables(connection)
    setup_sql, output_sql = aggregate_candidate_sql(candidate_id, unit)
    started = time.perf_counter()
    for statement in setup_sql:
        connection.execute(statement)
    connection.execute(f"CREATE TEMP TABLE governed_output AS {output_sql}")
    latency_ms = (time.perf_counter() - started) * 1000.0
    rows = connection.execute(
        "SELECT marker, governed_count FROM governed_output ORDER BY marker"
    ).fetchall()
    digest = hashlib.sha256(
        json.dumps(rows, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    lineage_digest = None
    if capture_lineage:
        lineage_rows = connection.execute(
            """
            SELECT dimension.marker, governed.row_id
            FROM governed_checkpoint AS governed
            INNER JOIN dimension
              ON governed.join_key = dimension.dimension_key
            ORDER BY dimension.marker, governed.row_id
            """
        ).fetchall()
        lineage_digest = hashlib.sha256(
            json.dumps(lineage_rows, default=str, separators=(",", ":")).encode()
        ).hexdigest()
    _drop_temporary_tables(connection)
    return latency_ms, digest, lineage_digest


def _plan_fingerprint(
    connection: Any,
    candidate_id: str,
    unit: GovernedAggregateUnit,
) -> str:
    """Bind admission to actually distinct DuckDB physical phase plans."""

    _drop_temporary_tables(connection)
    setup_sql, output_sql = aggregate_candidate_sql(candidate_id, unit)
    fingerprints: list[str] = []
    for statement in setup_sql:
        observation = observe_duckdb_plan(connection, statement, analyze=False)
        fingerprints.append(observation.fingerprint)
        connection.execute(statement)
    fingerprints.append(observe_duckdb_plan(connection, output_sql, analyze=False).fingerprint)
    _drop_temporary_tables(connection)
    return hashlib.sha256("|".join(fingerprints).encode()).hexdigest()


def _orders(
    candidate_ids: tuple[str, ...],
    repetitions: int,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    orders = list(itertools.permutations(candidate_ids)) * repetitions
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _environment(
    commit: str,
    dirty: bool,
    config: GovernedAggregateAdmissionConfig,
) -> dict[str, object]:
    """Capture the execution controls needed to reproduce timing."""

    try:
        duckdb_version = metadata.version("duckdb")
    except metadata.PackageNotFoundError:
        duckdb_version = "not-installed"
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "commit_hash": commit,
        "git_dirty": dirty,
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "duckdb_version": duckdb_version,
        "duckdb_threads": config.duckdb_threads,
        "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
        "gpu_acceleration": False,
    }


def _analyze(
    measurements: list[dict[str, object]],
    config: GovernedAggregateAdmissionConfig,
) -> dict[str, object]:
    """Authorize winner diversity only from paired hierarchical intervals."""

    families: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in measurements:
        families[str(row["scenario_id"])].append(row)
    scenario_results: list[dict[str, object]] = []
    winners: list[str] = []
    left, right = config.candidate_ids
    for scenario_id, rows in sorted(families.items()):
        ratios: dict[int, list[float]] = defaultdict(list)
        blocks: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in rows:
            blocks[(int(str(row["seed"])), int(str(row["block_index"])))][
                str(row["candidate_id"])
            ] = float(str(row["latency_ms"]))
        for (seed, _), values in sorted(blocks.items()):
            ratios[seed].append(math.log(values[left] / values[right]))
        stable = int.from_bytes(
            hashlib.sha256(f"{config.bootstrap_seed}:{scenario_id}".encode()).digest()[:8],
            "big",
        )
        point, lower, upper = hierarchical_paired_log_ratio_ci(
            ratios,
            confidence_level=config.confidence_level,
            repetitions=config.bootstrap_draws,
            seed=stable,
        )
        conclusion = classify_ratio_interval(lower, upper, config.practical_tie_fraction)
        winner = None
        if conclusion == "LEFT_MATERIALLY_FASTER":
            winner = left
        elif conclusion == "LEFT_MATERIALLY_SLOWER":
            winner = right
        if winner is not None:
            winners.append(winner)
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "left_over_right_ratio": point,
                "confidence_interval": [lower, upper],
                "conclusion": conclusion,
                "singleton_winner": winner,
            }
        )
    counts = Counter(winners)
    conclusive_rate = len(winners) / max(len(scenario_results), 1)
    gates = {
        "two_distinct_singleton_winners": len(counts) == 2,
        "minimum_conclusive_scenario_rate": (
            conclusive_rate >= config.minimum_conclusive_scenario_rate
        ),
    }
    passed = all(gates.values())
    return {
        "status": (
            "PASS_GOVERNED_AGGREGATE_OPTIMIZER_ADMISSION"
            if passed
            else "FAIL_GOVERNED_AGGREGATE_OPTIMIZER_ADMISSION_RETAIN"
        ),
        "singleton_winner_counts": dict(sorted(counts.items())),
        "conclusive_scenario_rate": conclusive_rate,
        "gates": gates,
        "scenario_results": scenario_results,
    }


def run_governed_aggregate_admission(
    config: GovernedAggregateAdmissionConfig,
    *,
    project_root: Path,
    config_path: Path,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Execute a clean, paired, resumable-free admission audit."""

    import duckdb

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Frozen aggregate admission requires a clean Git worktree")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    units = governed_aggregate_units(config)
    total_blocks = len(units) * config.blocks_per_unit
    measurements: list[dict[str, object]] = []
    unit_records: list[dict[str, object]] = []
    started = time.perf_counter()
    done = 0
    for unit in units:
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(f"SET threads TO {config.duckdb_threads}")
            connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
            cardinalities = _create_data(connection, unit)
            planning = plan_governed_aggregate(
                GovernedAggregateStatistics(
                    governed_rows=cardinalities["governed_rows"],
                    governed_keys=cardinalities["governed_keys"],
                )
            )
            if planning.nondominated_candidate_ids != config.candidate_ids:
                raise ValueError(
                    f"Aggregate candidate space collapsed before timing: {unit.unit_id}"
                )
            plans = {
                candidate_id: _plan_fingerprint(connection, candidate_id, unit)
                for candidate_id in config.candidate_ids
            }
            if len(set(plans.values())) != len(config.candidate_ids):
                raise ValueError(f"Aggregate physical plans collapsed: {unit.unit_id}")

            semantic_evidence = {
                candidate_id: _run_candidate(
                    connection,
                    candidate_id,
                    unit,
                    capture_lineage=True,
                )
                for candidate_id in config.candidate_ids
            }
            if len({item[1] for item in semantic_evidence.values()}) != 1:
                raise ValueError(f"Aggregate semantic results differ: {unit.unit_id}")
            if len({item[2] for item in semantic_evidence.values()}) != 1:
                raise ValueError(f"Aggregate semantic lineage differs: {unit.unit_id}")

            stable = int.from_bytes(hashlib.sha256(unit.unit_id.encode()).digest()[:4], "big")
            for order in _orders(
                config.candidate_ids,
                config.warmup_rounds_per_permutation,
                config.order_seed + stable,
            ):
                digests = [
                    _run_candidate(connection, candidate_id, unit)[1] for candidate_id in order
                ]
                if len(set(digests)) != 1:
                    raise ValueError(f"Aggregate warmup results differ: {unit.unit_id}")

            for block_index, order in enumerate(
                _orders(
                    config.candidate_ids,
                    config.measured_rounds_per_permutation,
                    config.order_seed + stable + 1,
                )
            ):
                block_rows: list[dict[str, object]] = []
                for position, candidate_id in enumerate(order):
                    latency_ms, result_digest, _ = _run_candidate(connection, candidate_id, unit)
                    block_rows.append(
                        {
                            "scenario_id": unit.scenario_id,
                            "unit_id": unit.unit_id,
                            "seed": unit.seed,
                            "candidate_id": candidate_id,
                            "block_index": block_index,
                            "order_position": position,
                            "permutation_id": "->".join(order),
                            "latency_ms": latency_ms,
                            "result_digest": result_digest,
                        }
                    )
                if len({row["result_digest"] for row in block_rows}) != 1:
                    raise ValueError(f"Aggregate measured results differ: {unit.unit_id}")
                measurements.extend(block_rows)
                done += 1
                if progress is not None:
                    progress(
                        done,
                        total_blocks,
                        f"{unit.unit_id} block={block_index + 1}",
                        time.perf_counter() - started,
                    )
            unit_records.append(
                {
                    "unit": asdict(unit),
                    "actual_cardinalities": cardinalities,
                    "planning": asdict(planning),
                    "plan_fingerprints": plans,
                    "result_digest": next(iter(semantic_evidence.values()))[1],
                    "lineage_digest": next(iter(semantic_evidence.values()))[2],
                }
            )
        finally:
            connection.close()

    fieldnames = list(measurements[0])
    with (output_dir / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(measurements)
    analysis = _analyze(measurements, config)
    summary = {
        **analysis,
        "run_id": run_id,
        "commit_hash": commit,
        "git_dirty": dirty,
        "query_family": "governed_join_aggregate_v1",
        "config_path": config_path.resolve().relative_to(root).as_posix(),
        "unit_count": len(units),
        "paired_block_count": total_blocks,
        "candidate_execution_count": len(measurements),
        "units": unit_records,
        "environment": _environment(commit, dirty, config),
    }
    _atomic_json(output_dir / "summary.json", summary)
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_id, "status": analysis["status"]},
    )
    return output_dir
