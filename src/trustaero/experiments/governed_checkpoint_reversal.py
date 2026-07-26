"""EA-1 pilot for governance-checkpoint placement reversals.

Both candidates satisfy the same logical query and place a required materialized
checkpoint before a cross-domain Join. ``policy_first`` hashes every sensitive
value but drops the wide value before the checkpoint. ``query_first`` applies a
cheap query predicate first and hashes fewer values, but must materialize raw
sensitive values. Governance feasibility is evaluated before timing comparison.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.execution import observe_duckdb_plan
from trustaero.experiments.execution_aware_oracle_stability import (
    classify_ratio_interval,
)
from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _environment,
    _git_state,
)
from trustaero.experiments.execution_flow_inference import (
    hierarchical_paired_log_ratio_ci,
)
from trustaero.optimizer.candidate_feasibility import (
    CandidateExposure,
    GovernanceFeasibilityPolicy,
    filter_feasible_candidates,
)

POLICY_FIRST = "policy_first_narrow_checkpoint"
QUERY_FIRST = "query_first_raw_checkpoint"
EA1_CANDIDATE_IDS = (POLICY_FIRST, QUERY_FIRST)


@dataclass(frozen=True, slots=True)
class GovernedCheckpointConfig:
    """Frozen synthetic dimensions and paired timing controls."""

    results_dir: str
    row_counts: tuple[int, ...]
    identifier_widths: tuple[int, ...]
    policy_selectivities: tuple[float, ...]
    query_selectivities: tuple[float, ...]
    seeds: tuple[int, ...]
    candidate_ids: tuple[str, ...]
    warmup_rounds: int
    repetitions_per_permutation: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    require_clean_git: bool
    # The timing engine is shared, but its scientific role must be explicit.
    # This prevents a consumed development run from being relabelled as holdout.
    experiment_role: str = "development_reversal"

    def __post_init__(self) -> None:
        dimensions: tuple[tuple[object, ...], ...] = (
            cast(tuple[object, ...], self.row_counts),
            cast(tuple[object, ...], self.identifier_widths),
            cast(tuple[object, ...], self.policy_selectivities),
            cast(tuple[object, ...], self.query_selectivities),
            cast(tuple[object, ...], self.seeds),
        )
        if any(not values or len(values) != len(set(values)) for values in dimensions):
            raise ValueError("EA-1 dimensions must be nonempty and unique")
        if any(value <= 0 for value in self.row_counts):
            raise ValueError("EA-1 row counts must be positive")
        if any(not 1 <= value <= 4096 for value in self.identifier_widths):
            raise ValueError("EA-1 widths must be in [1, 4096]")
        selectivities = self.policy_selectivities + self.query_selectivities
        if any(not 0.0 < value < 1.0 for value in selectivities):
            raise ValueError("EA-1 selectivities must be in (0, 1)")
        if len(self.seeds) < 3 or any(value < 0 for value in self.seeds):
            raise ValueError("EA-1 requires at least three nonnegative seeds")
        if self.candidate_ids != EA1_CANDIDATE_IDS:
            raise ValueError("EA-1 must retain both frozen checkpoint candidates")
        if self.warmup_rounds < 1 or self.repetitions_per_permutation < 15:
            raise ValueError("EA-1 requires warmup and 15 complete permutation repeats")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("EA-1 DuckDB limits are invalid")
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("EA-1 practical tie is invalid")
        if not 0.0 < self.confidence_level < 1.0 or self.bootstrap_draws < 1000:
            raise ValueError("EA-1 inference controls are invalid")
        if self.experiment_role not in {
            "development_reversal",
            "frozen_optimizer_holdout",
        }:
            raise ValueError("EA-1 experiment role is invalid")

    @property
    def measured_blocks_per_unit(self) -> int:
        return math.factorial(len(self.candidate_ids)) * self.repetitions_per_permutation


@dataclass(frozen=True, slots=True)
class GovernedCheckpointUnit:
    """One indivisible rows-width-policy-query-seed unit."""

    row_count: int
    identifier_width: int
    policy_selectivity: float
    query_selectivity: float
    seed: int

    @property
    def policy_cutoff(self) -> int:
        return round(self.policy_selectivity * 10_000)

    @property
    def query_cutoff(self) -> int:
        return round(self.query_selectivity * 10_000)

    @property
    def scenario_id(self) -> str:
        return (
            f"n{self.row_count}-w{self.identifier_width}-"
            f"p{self.policy_selectivity}-q{self.query_selectivity}"
        )

    @property
    def unit_id(self) -> str:
        return f"{self.scenario_id}-s{self.seed}"


def load_governed_checkpoint_config(path: str | Path) -> GovernedCheckpointConfig:
    """Load a versioned EA-1 JSON protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return GovernedCheckpointConfig(
        results_dir=str(payload["results_dir"]),
        row_counts=tuple(int(value) for value in payload["row_counts"]),
        identifier_widths=tuple(int(value) for value in payload["identifier_widths"]),
        policy_selectivities=tuple(float(value) for value in payload["policy_selectivities"]),
        query_selectivities=tuple(float(value) for value in payload["query_selectivities"]),
        seeds=tuple(int(value) for value in payload["seeds"]),
        candidate_ids=tuple(str(value) for value in payload["candidate_ids"]),
        warmup_rounds=int(payload["warmup_rounds"]),
        repetitions_per_permutation=int(payload["repetitions_per_permutation"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_draws=int(payload["bootstrap_draws"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        require_clean_git=bool(payload["require_clean_git"]),
        experiment_role=str(payload.get("experiment_role", "development_reversal")),
    )


def governed_checkpoint_units(
    config: GovernedCheckpointConfig,
) -> tuple[GovernedCheckpointUnit, ...]:
    """Expand the complete matrix without seed or scenario cherry-picking."""

    return tuple(
        GovernedCheckpointUnit(rows, width, policy, query, seed)
        for rows in config.row_counts
        for width in config.identifier_widths
        for policy in config.policy_selectivities
        for query in config.query_selectivities
        for seed in config.seeds
    )


def checkpoint_orders(
    candidate_ids: Sequence[str], repetitions: int, *, seed: int
) -> tuple[tuple[str, ...], ...]:
    """Use every permutation equally often, then shuffle complete blocks."""

    permutations = tuple(itertools.permutations(candidate_ids))
    orders = list(permutations) * repetitions
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _create_data(connection: Any, unit: GovernedCheckpointUnit) -> dict[str, int]:
    """Create exact-width values and independent deterministic query buckets."""

    for table in ("governed_output", "governance_checkpoint", "events", "dimension"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    blocks = math.ceil(unit.identifier_width / 32)
    dimension_rows = min(unit.row_count, 10_000)
    connection.execute(
        f"""
        CREATE TABLE events AS
        SELECT
            i::BIGINT AS row_id,
            left(
                repeat(md5(CAST(i + {unit.seed * 1_000_003} AS VARCHAR)), {blocks}),
                {unit.identifier_width}
            ) AS sensitive_value,
            (i % {dimension_rows})::BIGINT AS join_key,
            (hash(i + {unit.seed * 97_003}) % 10000)::INTEGER AS query_bucket
        FROM range({unit.row_count}) AS source(i)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE dimension AS
        SELECT i::BIGINT AS dimension_key, (i % 97)::BIGINT AS marker
        FROM range({dimension_rows}) AS source(i)
        """
    )
    observed = connection.execute(
        "SELECT count(*), min(length(sensitive_value)), max(length(sensitive_value)) FROM events"
    ).fetchone()
    if observed != (unit.row_count, unit.identifier_width, unit.identifier_width):
        raise ValueError(f"EA-1 width validation failed: {unit.unit_id}")
    policy_rows, query_rows, result_rows = connection.execute(
        f"""
        SELECT
            count(*) FILTER (
                WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
            ),
            count(*) FILTER (WHERE query_bucket < {unit.query_cutoff}),
            count(*) FILTER (
                WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
                  AND query_bucket < {unit.query_cutoff}
            )
        FROM events
        """
    ).fetchone()
    return {
        "policy_rows": int(policy_rows),
        "query_rows": int(query_rows),
        "result_rows": int(result_rows),
    }


def _candidate_sql(candidate_id: str, unit: GovernedCheckpointUnit) -> tuple[str, str]:
    """Return checkpoint and output SQL for one trusted candidate template."""

    if candidate_id == POLICY_FIRST:
        checkpoint = f"""
            CREATE TEMP TABLE governance_checkpoint AS
            SELECT row_id, join_key, query_bucket
            FROM events
            WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
        """
        predicate = f"checkpoint.query_bucket < {unit.query_cutoff}"
    elif candidate_id == QUERY_FIRST:
        checkpoint = f"""
            CREATE TEMP TABLE governance_checkpoint AS
            SELECT row_id, join_key, sensitive_value
            FROM events
            WHERE query_bucket < {unit.query_cutoff}
        """
        predicate = f"hash(checkpoint.sensitive_value) % 10000 < {unit.policy_cutoff}"
    else:
        raise ValueError(f"Unknown EA-1 candidate: {candidate_id}")
    output = f"""
        CREATE TEMP TABLE governed_output AS
        SELECT
            count(*)::BIGINT AS result_rows,
            sum(checkpoint.row_id)::HUGEINT AS row_id_sum,
            sum(dimension.marker)::HUGEINT AS marker_sum
        FROM governance_checkpoint AS checkpoint
        INNER JOIN dimension
          ON checkpoint.join_key = dimension.dimension_key
        WHERE {predicate}
    """
    return checkpoint, output


def _output_checksum(connection: Any) -> tuple[object, ...]:
    row = connection.execute(
        "SELECT result_rows, row_id_sum, marker_sum FROM governed_output"
    ).fetchone()
    if row is None:
        raise ValueError("EA-1 output checksum returned no row")
    return tuple(row)


def _execute_candidate(
    connection: Any,
    unit: GovernedCheckpointUnit,
    candidate_id: str,
    *,
    repeat_index: int,
    order_position: int,
    permutation_id: str,
) -> dict[str, object]:
    connection.execute("DROP TABLE IF EXISTS governed_output")
    connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
    checkpoint_sql, output_sql = _candidate_sql(candidate_id, unit)
    started = time.perf_counter()
    connection.execute(checkpoint_sql)
    connection.execute(output_sql)
    latency_ms = (time.perf_counter() - started) * 1000.0
    checksum = _output_checksum(connection)
    return {
        "scenario_id": unit.scenario_id,
        "unit_id": unit.unit_id,
        "seed": unit.seed,
        "candidate_id": candidate_id,
        "repeat_index": repeat_index,
        "order_position": order_position,
        "permutation_id": permutation_id,
        "latency_ms": latency_ms,
        "result_digest": _digest(checksum),
    }


def _profile_candidate(
    connection: Any,
    unit: GovernedCheckpointUnit,
    candidate_id: str,
    plan_dir: Path,
) -> dict[str, object]:
    """Capture both physical pipeline phases outside the formal timing sample."""

    connection.execute("DROP TABLE IF EXISTS governed_output")
    connection.execute("DROP TABLE IF EXISTS governance_checkpoint")
    checkpoint_sql, output_sql = _candidate_sql(candidate_id, unit)
    checkpoint = observe_duckdb_plan(connection, checkpoint_sql, analyze=True)
    output = observe_duckdb_plan(connection, output_sql, analyze=True)
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{candidate_id}-checkpoint.json").write_text(
        checkpoint.plan_json + "\n", encoding="utf-8"
    )
    (plan_dir / f"{candidate_id}-output.json").write_text(output.plan_json + "\n", encoding="utf-8")
    combined = _digest([checkpoint.fingerprint, output.fingerprint])
    return {
        "candidate_id": candidate_id,
        "combined_fingerprint": combined,
        "checkpoint_fingerprint": checkpoint.fingerprint,
        "output_fingerprint": output.fingerprint,
        "checkpoint_operators": list(checkpoint.operator_names),
        "output_operators": list(output.operator_names),
        "peak_buffer_memory_bytes": max(
            checkpoint.peak_buffer_memory_bytes, output.peak_buffer_memory_bytes
        ),
        "peak_temp_directory_bytes": max(
            checkpoint.peak_temp_directory_bytes, output.peak_temp_directory_bytes
        ),
    }


def _feasibility(actual: Mapping[str, int]) -> dict[str, object]:
    """Prove that hard raw-materialization policy is evaluated before cost."""

    exposures = (
        CandidateExposure(POLICY_FIRST, 0, 0),
        CandidateExposure(QUERY_FIRST, 0, actual["query_rows"]),
    )
    permissive = filter_feasible_candidates(
        exposures, GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None)
    )
    strict = filter_feasible_candidates(
        exposures, GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0)
    )
    if permissive.feasible_candidate_ids != EA1_CANDIDATE_IDS:
        raise ValueError("EA-1 permissive policy unexpectedly removed a candidate")
    if strict.feasible_candidate_ids != (POLICY_FIRST,):
        raise ValueError("EA-1 strict policy did not reject raw checkpoint")
    return {
        "permissive_feasible_candidate_ids": list(permissive.feasible_candidate_ids),
        "strict_feasible_candidate_ids": list(strict.feasible_candidate_ids),
        "strict_rejected_candidate_ids": list(strict.rejected_candidate_ids),
        "governance_before_cost": True,
    }


def _run_unit(
    config: GovernedCheckpointConfig,
    unit: GovernedCheckpointUnit,
    output_dir: Path,
    *,
    completed_blocks: int,
    total_blocks: int,
    started: float,
    progress_callback: Callable[[int, int, str, float], None] | None,
) -> dict[str, object]:
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = output_dir / "duckdb_temp" / unit.unit_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        escaped_temp = str(temp_dir.resolve()).replace("'", "''")
        connection.execute(f"SET temp_directory = '{escaped_temp}'")
        actual = _create_data(connection, unit)
        feasibility = _feasibility(actual)
        profiles = {
            candidate_id: _profile_candidate(
                connection,
                unit,
                candidate_id,
                output_dir / "plans" / unit.unit_id,
            )
            for candidate_id in config.candidate_ids
        }
        if len({str(profile["combined_fingerprint"]) for profile in profiles.values()}) != len(
            config.candidate_ids
        ):
            raise ValueError("EA-1 candidate physical plans are not distinct")

        order_seed = (
            config.order_seed
            + unit.seed
            + int.from_bytes(hashlib.sha256(unit.scenario_id.encode()).digest()[:4], "big")
        )
        warmups = checkpoint_orders(config.candidate_ids, config.warmup_rounds, seed=order_seed)
        for repeat_index, order in enumerate(warmups):
            for position, candidate_id in enumerate(order):
                _execute_candidate(
                    connection,
                    unit,
                    candidate_id,
                    repeat_index=repeat_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )

        orders = checkpoint_orders(
            config.candidate_ids,
            config.repetitions_per_permutation,
            seed=order_seed + 1,
        )
        measurements: list[dict[str, object]] = []
        for block_index, order in enumerate(orders):
            block_rows = [
                _execute_candidate(
                    connection,
                    unit,
                    candidate_id,
                    repeat_index=block_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )
                for position, candidate_id in enumerate(order)
            ]
            if len({str(row["result_digest"]) for row in block_rows}) != 1:
                raise ValueError("EA-1 equivalent candidates returned different results")
            measurements.extend(block_rows)
            if progress_callback is not None:
                progress_callback(
                    completed_blocks + block_index + 1,
                    total_blocks,
                    f"{unit.unit_id} block={block_index + 1}",
                    time.perf_counter() - started,
                )
        return {
            "unit": asdict(unit),
            "unit_id": unit.unit_id,
            "actual_cardinalities": actual,
            "estimated_checkpoint_bytes": {
                POLICY_FIRST: actual["policy_rows"] * 24,
                QUERY_FIRST: actual["query_rows"] * (16 + unit.identifier_width),
            },
            "feasibility": feasibility,
            "profiles": profiles,
            "measurements": measurements,
        }
    finally:
        connection.close()


def _write_measurements(output_dir: Path, payloads: Sequence[Mapping[str, Any]]) -> None:
    rows: list[dict[str, object]] = []
    for payload in payloads:
        unit = cast(dict[str, object], payload["unit"])
        actual = cast(dict[str, object], payload["actual_cardinalities"])
        for row in cast(list[dict[str, object]], payload["measurements"]):
            rows.append({**unit, **actual, **row})
    with (output_dir / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _analyze(output_dir: Path, config: GovernedCheckpointConfig) -> dict[str, object]:
    with (output_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        families[row["scenario_id"]].append(row)
    results: list[dict[str, object]] = []
    policy_wins = query_wins = 0
    for scenario_id, family_rows in sorted(families.items()):
        by_block: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in family_rows:
            by_block[(int(row["seed"]), int(row["repeat_index"]))][row["candidate_id"]] = float(
                row["latency_ms"]
            )
        ratios: dict[int, list[float]] = defaultdict(list)
        for (seed, _repeat), latencies in sorted(by_block.items()):
            if set(latencies) != set(EA1_CANDIDATE_IDS):
                raise ValueError(f"EA-1 incomplete paired block: {scenario_id}")
            ratios[seed].append(math.log(latencies[POLICY_FIRST] / latencies[QUERY_FIRST]))
        stable_seed = int.from_bytes(
            hashlib.sha256(f"{config.bootstrap_seed}:{scenario_id}".encode()).digest()[:8],
            "big",
        )
        point, lower, upper = hierarchical_paired_log_ratio_ci(
            ratios,
            confidence_level=config.confidence_level,
            repetitions=config.bootstrap_draws,
            seed=stable_seed,
        )
        conclusion = classify_ratio_interval(lower, upper, config.practical_tie_fraction)
        policy_wins += conclusion == "LEFT_MATERIALLY_FASTER"
        query_wins += conclusion == "LEFT_MATERIALLY_SLOWER"
        results.append(
            {
                "scenario_id": scenario_id,
                "policy_first_over_query_first_ratio": point,
                "confidence_interval": [lower, upper],
                "conclusion": conclusion,
                "seed_count": len(ratios),
                "paired_block_count": sum(len(values) for values in ratios.values()),
            }
        )
    if policy_wins and query_wins:
        discovery = "STABLE_BIDIRECTIONAL_REVERSAL_DISCOVERED"
    elif policy_wins or query_wins:
        discovery = "ONLY_ONE_STABLE_DIRECTION_DISCOVERED"
    else:
        discovery = "NO_STABLE_DIRECTION_DISCOVERED"
    is_holdout = config.experiment_role == "frozen_optimizer_holdout"
    result: dict[str, object] = {
        "status": "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY",
        "reversal_discovery": discovery,
        "scenario_count": len(results),
        "policy_first_winner_count": policy_wins,
        "query_first_winner_count": query_wins,
        "inconclusive_count": len(results) - policy_wins - query_wins,
        "scenario_results": results,
        "governance_before_cost": True,
        "experiment_role": config.experiment_role,
        "optimizer_trained": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This run measures a pre-registered frozen-optimizer holdout matrix. "
            "Its timing summary alone does not evaluate the optimizer; the separately "
            "frozen holdout checker must bind and score the frozen model."
            if is_holdout
            else "EA-1 is a pre-registered development discovery pilot. It tests "
            "whether governance-checkpoint placement yields stable plan reversals; "
            "it does not evaluate a trained optimizer or constitute final holdout "
            "evidence."
        ),
    }
    _atomic_json(output_dir / "summary.json", result)
    return result


def run_governed_checkpoint_reversal(
    config: GovernedCheckpointConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume EA-1 with one atomic checkpoint per complete data unit."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("EA-1 requires a clean Git commit")
    config_payload = asdict(config)
    config_digest = _digest(config_payload)
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    results_root = root / config.results_dir
    output_dir = results_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    if resume_run_id:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        environment = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != config_digest:
            raise ValueError("EA-1 resume configuration changed")
        if environment.get("commit_hash") != commit:
            raise ValueError("EA-1 resume commit changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_units": [],
            "status": "running",
        }
        _atomic_json(output_dir / "config.json", config_payload)
        _atomic_json(
            output_dir / "environment.json",
            _environment(commit, dirty, cast(Any, config)),
        )
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})

    units = governed_checkpoint_units(config)
    completed = set(cast(list[str], checkpoint["completed_units"]))
    remaining_units = tuple(unit for unit in units if unit.unit_id not in completed)
    started = time.perf_counter()
    # Progress and ETA describe the current session. On resume this avoids
    # dividing a short new elapsed time by historical completed blocks.
    blocks_done = 0
    total_blocks = len(remaining_units) * config.measured_blocks_per_unit
    for unit in remaining_units:
        payload = _run_unit(
            config,
            unit,
            output_dir,
            completed_blocks=blocks_done,
            total_blocks=total_blocks,
            started=started,
            progress_callback=progress_callback,
        )
        _atomic_json(output_dir / "units" / f"{unit.unit_id}.json", payload)
        completed.add(unit.unit_id)
        blocks_done += config.measured_blocks_per_unit
        checkpoint["completed_units"] = sorted(completed)
        checkpoint["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_json(checkpoint_path, checkpoint)
    payloads = [
        json.loads((output_dir / "units" / f"{unit.unit_id}.json").read_text()) for unit in units
    ]
    _write_measurements(output_dir, payloads)
    summary = _analyze(output_dir, config)
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    checkpoint["reversal_discovery"] = summary["reversal_discovery"]
    _atomic_json(checkpoint_path, checkpoint)
    return output_dir
