"""Frozen real-distribution transfer for the governed pipeline optimizer.

The experiment keeps native event times, Join keys, key-frequency skew, and
official dimension attributes from BTS and NYC TLC.  It deterministically adds
only the governance controls that public data does not naturally contain:
sensitive-payload width, policy selectivity, query selectivity, and dimension
coverage.  The frozen synthetic cost model is never fitted in this module.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from trustaero.experiments.execution_aware_calibration import (
    CalibrationObservation,
)
from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _environment,
    _git_state,
)
from trustaero.experiments.governed_pipeline_admission import _analyze
from trustaero.experiments.governed_pipeline_cost_calibration import (
    EQUIVALENCE_GROUP,
    _selection_metrics,
    fixed_candidate_baselines,
)
from trustaero.experiments.governed_pipeline_cost_holdout import (
    _select_with_frozen_model,
)
from trustaero.experiments.governed_pipeline_execution import (
    build_executable_governed_pipeline,
    execute_governed_pipeline,
    observe_governed_pipeline_plan,
)
from trustaero.experiments.real_governed_checkpoint_transfer import (
    RealCheckpointSource,
    _sql_path,
    _validate_sources,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_pipeline_space import (
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    GovernedPipelineStatistics,
    build_governed_pipeline_candidates,
    plan_governed_pipeline,
)

REAL_PIPELINE_CANDIDATE_IDS = (
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    JOIN_FIRST_MASKED_CHECKPOINT,
)
DatasetName = Literal["bts", "nyc_tlc"]


def _sha256(path: Path) -> str:
    """Return the lowercase digest used to bind frozen local artifacts."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class RealGovernedPipelineProfile:
    """One pre-registered governance workload over a real distribution."""

    profile_id: str
    identifier_width: int
    policy_selectivity: float
    query_selectivity: float
    dimension_coverage: float

    def __post_init__(self) -> None:
        if self.identifier_width <= 0:
            raise ValueError("Sensitive width must be positive")
        for value in (
            self.policy_selectivity,
            self.query_selectivity,
            self.dimension_coverage,
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError("Real pipeline selectivities must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class RealGovernedPipelineConfig:
    """Immutable real-data split and paired timing protocol."""

    results_dir: str
    sources: tuple[RealCheckpointSource, ...]
    profiles: tuple[RealGovernedPipelineProfile, ...]
    row_count: int
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
    minimum_distinct_singleton_winners: int
    maximum_dominant_singleton_winner_fraction: float
    require_no_material_carryover: bool
    require_clean_git: bool
    experiment_role: str

    def __post_init__(self) -> None:
        if not self.sources or len({item.source_id for item in self.sources}) != len(self.sources):
            raise ValueError("Real sources must be nonempty and unique")
        if not self.profiles or len({item.profile_id for item in self.profiles}) != len(
            self.profiles
        ):
            raise ValueError("Real profiles must be nonempty and unique")
        if self.row_count != 120_000:
            raise ValueError("Frozen real transfer must remain at 120000 rows")
        if len(self.seeds) < 3 or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("Real transfer requires at least three unique seeds")
        if self.candidate_ids != REAL_PIPELINE_CANDIDATE_IDS:
            raise ValueError("Frozen real candidate space changed")
        if self.measured_rounds_per_permutation < 5:
            raise ValueError("Paired timing protocol is too weak")
        if self.experiment_role != "frozen_real_pipeline_optimizer_transfer":
            raise ValueError("Scientific role changed")

    @property
    def measured_blocks_per_unit(self) -> int:
        return math.factorial(len(self.candidate_ids)) * self.measured_rounds_per_permutation


@dataclass(frozen=True, slots=True)
class RealGovernedPipelineUnit:
    """One source-profile-seed unit that can be resumed atomically."""

    dataset: DatasetName
    month: str
    event_path: str
    dimension_path: str
    profile_id: str
    row_count: int
    identifier_width: int
    policy_selectivity: float
    query_selectivity: float
    dimension_coverage: float
    seed: int

    @property
    def policy_cutoff(self) -> int:
        return round(self.policy_selectivity * 10_000)

    @property
    def query_cutoff(self) -> int:
        return round(self.query_selectivity * 10_000)

    @property
    def scenario_id(self) -> str:
        return f"{self.dataset}-{self.month}-{self.profile_id}"

    @property
    def unit_id(self) -> str:
        return f"{self.scenario_id}-s{self.seed}"


@dataclass(frozen=True, slots=True)
class RealGovernedPipelineEvaluationConfig:
    """Frozen model binding and one-shot real-transfer acceptance gates."""

    results_dir: str
    measurement_results_dir: str
    model_path: str
    model_sha256: str
    development_calibration_path: str
    development_calibration_sha256: str
    expected_sources: tuple[str, ...]
    expected_profiles: tuple[str, ...]
    expected_row_count: int
    expected_seeds: tuple[int, ...]
    minimum_oracle_set_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_selected_candidate_count: int
    require_not_worse_than_best_fixed_mean: bool
    require_not_worse_than_best_fixed_p95: bool


def load_real_governed_pipeline_config(
    path: str | Path,
) -> RealGovernedPipelineConfig:
    """Load and validate the pre-registered measurement matrix."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["sources"] = tuple(RealCheckpointSource(**item) for item in payload["sources"])
    payload["profiles"] = tuple(RealGovernedPipelineProfile(**item) for item in payload["profiles"])
    payload["seeds"] = tuple(int(value) for value in payload["seeds"])
    payload["candidate_ids"] = tuple(str(value) for value in payload["candidate_ids"])
    return RealGovernedPipelineConfig(**payload)


def load_real_governed_pipeline_evaluation_config(
    path: str | Path,
) -> RealGovernedPipelineEvaluationConfig:
    """Load the frozen one-shot evaluation gates."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["expected_sources"] = tuple(payload["expected_sources"])
    payload["expected_profiles"] = tuple(payload["expected_profiles"])
    payload["expected_seeds"] = tuple(int(value) for value in payload["expected_seeds"])
    return RealGovernedPipelineEvaluationConfig(**payload)


def real_governed_pipeline_units(
    config: RealGovernedPipelineConfig,
) -> tuple[RealGovernedPipelineUnit, ...]:
    """Expand every declared month and profile without cherry-picking."""

    return tuple(
        RealGovernedPipelineUnit(
            dataset=source.dataset,
            month=source.month,
            event_path=source.event_path,
            dimension_path=source.dimension_path,
            profile_id=profile.profile_id,
            row_count=config.row_count,
            identifier_width=profile.identifier_width,
            policy_selectivity=profile.policy_selectivity,
            query_selectivity=profile.query_selectivity,
            dimension_coverage=profile.dimension_coverage,
            seed=seed,
        )
        for source in config.sources
        for profile in config.profiles
        for seed in config.seeds
    )


def _drop_execution_tables(connection: Any) -> None:
    """Remove the complete superset of candidate-created tables."""

    for table in (
        "governed_output",
        "pipeline_checkpoint",
        "raw_query_checkpoint",
        "raw_join_checkpoint",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _create_real_data(
    connection: Any,
    root: Path,
    unit: RealGovernedPipelineUnit,
) -> dict[str, int]:
    """Build a deterministic sample while retaining native skew and keys."""

    for name in ("events", "dimension", "dimension_native", "real_sample"):
        connection.execute(f"DROP TABLE IF EXISTS {name}")
    connection.execute("DROP VIEW IF EXISTS eligible_real_events")
    event_path = _sql_path(root, unit.event_path)
    dimension_path = _sql_path(root, unit.dimension_path)
    if unit.dataset == "bts":
        connection.execute(
            f"""
            CREATE TEMP VIEW eligible_real_events AS
            SELECT CAST(FlightDate AS TIMESTAMP) AS event_time,
                   CAST(OriginAirportID AS BIGINT) AS join_key,
                   concat_ws('|', coalesce(CAST(Tail_Number AS VARCHAR), 'UNKNOWN'),
                     coalesce(CAST(Reporting_Airline AS VARCHAR), 'UNKNOWN'),
                     coalesce(CAST(Flight_Number_Reporting_Airline AS VARCHAR), '0'),
                     CAST(FlightDate AS VARCHAR), coalesce(CAST(Origin AS VARCHAR), 'UNKNOWN'),
                     coalesce(CAST(Dest AS VARCHAR), 'UNKNOWN')) AS native_token
            FROM read_parquet({event_path})
            WHERE FlightDate IS NOT NULL AND OriginAirportID IS NOT NULL
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE dimension_native AS
            SELECT CAST(airport_id AS BIGINT) AS dimension_key,
                   CAST(hash(airport_code, city_name, state_code) % 97 AS BIGINT) AS marker
            FROM read_parquet({dimension_path})
            """
        )
    elif unit.dataset == "nyc_tlc":
        connection.execute(
            f"""
            CREATE TEMP VIEW eligible_real_events AS
            SELECT CAST(tpep_pickup_datetime AS TIMESTAMP) AS event_time,
                   CAST(PULocationID AS BIGINT) AS join_key,
                   concat_ws('|', coalesce(CAST(VendorID AS VARCHAR), '0'),
                     CAST(tpep_pickup_datetime AS VARCHAR),
                     CAST(tpep_dropoff_datetime AS VARCHAR),
                     coalesce(CAST(PULocationID AS VARCHAR), '0'),
                     coalesce(CAST(DOLocationID AS VARCHAR), '0'),
                     coalesce(CAST(total_amount AS VARCHAR), '0')) AS native_token
            FROM read_parquet({event_path})
            WHERE tpep_pickup_datetime IS NOT NULL
              AND PULocationID IS NOT NULL AND PULocationID > 0
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE dimension_native AS
            SELECT CAST(LocationID AS BIGINT) AS dimension_key,
                   CAST(hash(Borough, Zone, service_zone) % 97 AS BIGINT) AS marker
            FROM read_parquet({dimension_path})
            """
        )
    else:  # pragma: no cover
        raise ValueError(f"Unknown real dataset: {unit.dataset}")

    connection.execute(
        f"""
        CREATE TEMP TABLE real_sample AS
        SELECT * FROM eligible_real_events
        USING SAMPLE reservoir({unit.row_count} ROWS) REPEATABLE({unit.seed})
        """
    )
    sampled_rows = int(connection.execute("SELECT count(*) FROM real_sample").fetchone()[0])
    if sampled_rows != unit.row_count:
        raise ValueError(f"Real source is too small: {unit.unit_id}={sampled_rows}")

    blocks = math.ceil(unit.identifier_width / 32)
    connection.execute(
        f"""
        CREATE TEMP TABLE events AS
        WITH ranked AS (
          SELECT row_number() OVER (ORDER BY event_time, join_key, native_token) - 1 AS row_id,
                 event_time, join_key, native_token
          FROM real_sample
        )
        SELECT CAST(row_id AS BIGINT) AS row_id,
               left(repeat(md5(native_token || ':' || CAST(row_id AS VARCHAR)),
                           {blocks}), {unit.identifier_width}) AS sensitive_value,
               CAST(join_key AS BIGINT) AS join_key,
               CAST(floor(row_id * 10000.0 / {unit.row_count}) AS INTEGER) AS query_bucket
        FROM ranked
        """
    )

    # Select complete native dimension keys until their cumulative real-event
    # mass reaches the declared coverage. This controls coverage without
    # flattening or synthesizing the native key-frequency distribution.
    target_rows = round(unit.row_count * unit.dimension_coverage)
    connection.execute(
        f"""
        CREATE TEMP TABLE dimension AS
        WITH frequencies AS (
          SELECT join_key AS dimension_key, count(*) AS event_rows
          FROM events GROUP BY join_key
        ), ordered AS (
          SELECT dimension_key, event_rows,
                 sum(event_rows) OVER (
                   ORDER BY hash(dimension_key + {unit.seed}), dimension_key
                 ) AS cumulative_rows
          FROM frequencies
        ), selected AS (
          SELECT dimension_key FROM ordered
          WHERE cumulative_rows - event_rows < {target_rows}
        )
        SELECT native.dimension_key, native.marker
        FROM dimension_native AS native
        INNER JOIN selected USING (dimension_key)
        """
    )
    row = connection.execute(
        f"""
        SELECT count(*) FILTER (WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}),
               count(*) FILTER (WHERE query_bucket < {unit.query_cutoff}),
               count(*) FILTER (WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
                                  AND query_bucket < {unit.query_cutoff}),
               count(*) FILTER (WHERE query_bucket < {unit.query_cutoff}
                                  AND join_key IN
                                      (SELECT dimension_key FROM dimension)),
               count(*) FILTER (WHERE hash(sensitive_value) % 10000 < {unit.policy_cutoff}
                                  AND query_bucket < {unit.query_cutoff}
                                  AND join_key IN
                                      (SELECT dimension_key FROM dimension))
        FROM events
        """
    ).fetchone()
    if row is None:
        raise ValueError("Real cardinality query returned no row")
    actual = {
        "policy_rows": int(row[0]),
        "query_rows": int(row[1]),
        "governed_rows": int(row[2]),
        "query_join_rows": int(row[3]),
        "result_rows": int(row[4]),
    }
    observed_join = actual["query_join_rows"] / max(actual["query_rows"], 1)
    if not 0.5 <= observed_join <= 0.9:
        raise ValueError(
            f"Observed Join match rate left frozen support: {unit.unit_id}={observed_join:.3f}"
        )
    return actual


def _orders(
    candidate_ids: tuple[str, ...],
    repetitions: int,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    orders = list(itertools.permutations(candidate_ids)) * repetitions
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _execute_timed(
    connection: Any,
    candidate: Any,
    unit: RealGovernedPipelineUnit,
    *,
    block_index: int,
    order_position: int,
    permutation_id: str,
) -> dict[str, object]:
    """Time database work and derive result/lineage evidence afterwards."""

    _drop_execution_tables(connection)
    started = time.perf_counter()
    for statement in candidate.setup_sql:
        connection.execute(statement)
    connection.execute(f"CREATE TEMP TABLE governed_output AS {candidate.output_sql}")
    latency_ms = (time.perf_counter() - started) * 1000.0
    rows = tuple(
        connection.execute(
            "SELECT row_id, dimension_key, marker, masked_value "
            "FROM governed_output ORDER BY row_id, dimension_key"
        ).fetchall()
    )
    encoded = json.dumps(rows, default=str, separators=(",", ":")).encode()
    result_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    lineage = tuple((int(row[0]), int(row[1])) for row in rows)
    lineage_digest = (
        "sha256:" + hashlib.sha256(json.dumps(lineage, separators=(",", ":")).encode()).hexdigest()
    )
    _drop_execution_tables(connection)
    return {
        "scenario_id": unit.scenario_id,
        "unit_id": unit.unit_id,
        "seed": unit.seed,
        "candidate_id": candidate.candidate_id,
        "block_index": block_index,
        "order_position": order_position,
        "permutation_id": permutation_id,
        "latency_ms": latency_ms,
        # The shared paired-order carryover checker uses this historical field
        # name.  Keep both aliases bound to the exact same timer observation.
        "client_materialization_latency_ms": latency_ms,
        "result_digest": result_digest,
        "lineage_digest": lineage_digest,
    }


def _run_unit(
    config: RealGovernedPipelineConfig,
    unit: RealGovernedPipelineUnit,
    root: Path,
    *,
    done_before: int,
    total_blocks: int,
    started: float,
    progress: Callable[[int, int, str, float], None] | None,
) -> dict[str, object]:
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        actual = _create_real_data(connection, root, unit)
        pipeline_statistics = GovernedPipelineStatistics(
            input_rows=unit.row_count,
            estimated_policy_rows=actual["policy_rows"],
            estimated_query_rows=actual["query_rows"],
            estimated_governed_rows=actual["governed_rows"],
            estimated_query_join_rows=actual["query_join_rows"],
            estimated_result_rows=actual["result_rows"],
            sensitive_width_bytes=float(unit.identifier_width),
        )
        planning = plan_governed_pipeline(
            pipeline_statistics,
            GovernanceFeasibilityPolicy(
                "real-checkpoint-required",
                None,
                None,
                require_governance_checkpoint=True,
            ),
        )
        if planning.nondominated_candidate_ids != config.candidate_ids:
            raise ValueError(f"Candidate space collapsed: {unit.unit_id}")
        candidates = {
            candidate_id: build_executable_governed_pipeline(
                candidate_id,
                cast(Any, unit),
            )
            for candidate_id in config.candidate_ids
        }
        plans = {
            candidate_id: observe_governed_pipeline_plan(connection, candidate)
            for candidate_id, candidate in candidates.items()
        }
        if len({value.combined_fingerprint for value in plans.values()}) != 3:
            raise ValueError(f"Physical plans collapsed: {unit.unit_id}")

        stable = int.from_bytes(hashlib.sha256(unit.unit_id.encode()).digest()[:4], "big")
        for order in _orders(
            config.candidate_ids,
            config.warmup_rounds_per_permutation,
            config.order_seed + stable,
        ):
            evidence = [
                execute_governed_pipeline(connection, candidates[candidate_id])
                for candidate_id in order
            ]
            if len({item.result_digest for item in evidence}) != 1:
                raise ValueError(f"Warmup result mismatch: {unit.unit_id}")
            if len({item.lineage_digest for item in evidence}) != 1:
                raise ValueError(f"Warmup lineage mismatch: {unit.unit_id}")

        measurements: list[dict[str, object]] = []
        orders = _orders(
            config.candidate_ids,
            config.measured_rounds_per_permutation,
            config.order_seed + stable + 1,
        )
        for block_index, order in enumerate(orders):
            block = [
                _execute_timed(
                    connection,
                    candidates[candidate_id],
                    unit,
                    block_index=block_index,
                    order_position=position,
                    permutation_id="->".join(order),
                )
                for position, candidate_id in enumerate(order)
            ]
            if len({item["result_digest"] for item in block}) != 1:
                raise ValueError(f"Measured result mismatch: {unit.unit_id}")
            if len({item["lineage_digest"] for item in block}) != 1:
                raise ValueError(f"Measured lineage mismatch: {unit.unit_id}")
            measurements.extend(block)
            if progress is not None:
                progress(
                    done_before + block_index + 1,
                    total_blocks,
                    f"{unit.unit_id} block={block_index + 1}",
                    time.perf_counter() - started,
                )
        return {
            "unit": asdict(unit),
            "actual_cardinalities": actual,
            "planning": asdict(planning),
            "plan_fingerprints": {key: value.combined_fingerprint for key, value in plans.items()},
            "measurements": measurements,
            "field_provenance": {
                "event_time": "native",
                "join_key_and_frequency": "native",
                "dimension_attributes": "native",
                "dimension_membership": "deterministic_controlled_coverage",
                "sensitive_payload": "deterministic_controlled_from_native_token",
                "query_bucket": "native_event_time_rank",
                "policy_bucket": "hash_of_controlled_sensitive_payload",
            },
        }
    finally:
        connection.close()


def run_real_governed_pipeline_transfer(
    config: RealGovernedPipelineConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume the frozen real transfer one complete unit at a time."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Real transfer requires a clean Git commit")
    source_records = _validate_sources(root, config.sources)
    results_root = root / config.results_dir
    if resume_run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = results_root / run_id
        output.mkdir(parents=True, exist_ok=False)
        (output / "units").mkdir()
        _atomic_json(output / "config.json", asdict(config))
        environment = _environment(commit, dirty, cast(Any, config))
        environment.update(
            {
                "config_path": str(config_path.resolve()),
                "config_sha256": _sha256(config_path),
                "source_records": source_records,
                "cache_protocol": "warm_same_unit_connection",
                "gpu_acceleration": False,
            }
        )
        _atomic_json(output / "environment.json", environment)
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    else:
        output = results_root / resume_run_id
        frozen = json.loads((output / "config.json").read_text(encoding="utf-8"))
        if frozen != json.loads(json.dumps(asdict(config))):
            raise ValueError("Resume configuration changed")

    units = real_governed_pipeline_units(config)
    total_blocks = len(units) * config.measured_blocks_per_unit
    started = time.perf_counter()
    completed = 0
    for unit in units:
        unit_path = output / "units" / f"{unit.unit_id}.json"
        if unit_path.exists():
            completed += config.measured_blocks_per_unit
            continue
        payload = _run_unit(
            config,
            unit,
            root,
            done_before=completed,
            total_blocks=total_blocks,
            started=started,
            progress=progress,
        )
        _atomic_json(unit_path, payload)
        completed += config.measured_blocks_per_unit

    rows: list[dict[str, object]] = []
    for unit in units:
        payload = json.loads(
            (output / "units" / f"{unit.unit_id}.json").read_text(encoding="utf-8")
        )
        for measurement in payload["measurements"]:
            # Runs completed before the analysis-only compatibility fix contain
            # ``latency_ms`` but not the legacy carryover-checker alias.  Adding
            # the alias here does not change, recompute, or discard any timing.
            normalized_measurement = dict(measurement)
            normalized_measurement.setdefault(
                "client_materialization_latency_ms",
                normalized_measurement["latency_ms"],
            )
            rows.append(
                {
                    **payload["unit"],
                    **payload["actual_cardinalities"],
                    **normalized_measurement,
                }
            )
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    analysis = _analyze(
        [{key: str(value) for key, value in row.items()} for row in rows],
        cast(Any, config),
    )
    measurement_environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))
    measurement_commit = str(measurement_environment["commit_hash"])
    analysis.update(
        {
            "status": "PASS_REAL_GOVERNED_PIPELINE_TRANSFER_MEASUREMENT_INTEGRITY",
            "winner_analysis_status": analysis["status"],
            "dataset_count": len({item.dataset for item in config.sources}),
            "source_month_count": len(config.sources),
            "unit_count": len(units),
            "native_distribution_preserved": [
                "event_time",
                "join_key_frequency",
                "dimension_attributes",
            ],
            "controlled_governance_fields": [
                "sensitive_payload_width",
                "policy_selectivity",
                "query_selectivity",
                "dimension_coverage",
            ],
            "paper_optimizer_performance_claim_authorized": False,
            "measurement_commit_hash": measurement_commit,
            "analysis_commit_hash": commit,
            "post_measurement_analysis_only_fix_applied": measurement_commit != commit,
            "raw_unit_measurements_recomputed": False,
        }
    )
    _atomic_json(output / "summary.json", analysis)
    return output


def _load_real_observations(
    run_dir: Path,
) -> tuple[
    tuple[CalibrationObservation, ...],
    dict[tuple[str, int, str], tuple[str, ...]],
    dict[str, object],
]:
    observations: list[CalibrationObservation] = []
    legal: dict[tuple[str, int, str], tuple[str, ...]] = {}
    paths = sorted((run_dir / "units").glob("*.json"))
    if not paths:
        raise ValueError("Real transfer contains no units")
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        measurements = payload["measurements"]
        if len(measurements) != 90:
            raise ValueError(f"Incomplete real timing unit: {path.name}")
        if len({item["result_digest"] for item in measurements}) != 1:
            raise ValueError(f"Real result mismatch: {path.name}")
        if len({item["lineage_digest"] for item in measurements}) != 1:
            raise ValueError(f"Real lineage mismatch: {path.name}")
        if len(set(payload["plan_fingerprints"].values())) != 3:
            raise ValueError(f"Real physical plans collapsed: {path.name}")
        counts = Counter(item["candidate_id"] for item in measurements)
        if set(counts.values()) != {30}:
            raise ValueError(f"Real schedule is unbalanced: {path.name}")
        unit = payload["unit"]
        actual = payload["actual_cardinalities"]
        scenario_id = str(measurements[0]["scenario_id"])
        seed = int(unit["seed"])
        pipeline_statistics = GovernedPipelineStatistics(
            input_rows=int(unit["row_count"]),
            estimated_policy_rows=int(actual["policy_rows"]),
            estimated_query_rows=int(actual["query_rows"]),
            estimated_governed_rows=int(actual["governed_rows"]),
            estimated_query_join_rows=int(actual["query_join_rows"]),
            estimated_result_rows=int(actual["result_rows"]),
            sensitive_width_bytes=float(unit["identifier_width"]),
        )
        profiles = {
            candidate.candidate_id: candidate.profile
            for candidate in build_governed_pipeline_candidates(pipeline_statistics)
            if candidate.candidate_id in counts
        }
        group = (scenario_id, seed, EQUIVALENCE_GROUP)
        legal[group] = tuple(payload["planning"]["nondominated_candidate_ids"])
        for candidate_id, profile in sorted(profiles.items()):
            latencies = [
                float(item["latency_ms"])
                for item in measurements
                if item["candidate_id"] == candidate_id
            ]
            observations.append(
                CalibrationObservation(
                    scenario_id=scenario_id,
                    seed=seed,
                    equivalence_group=EQUIVALENCE_GROUP,
                    candidate_id=candidate_id,
                    latency_ms=statistics.median(latencies),
                    features=profile.work_metrics,
                )
            )
    integrity: dict[str, object] = {
        "unit_count": len(paths),
        "observation_count": len(observations),
        "measurement_row_count": len(observations) * 30,
        "result_equivalence_passed": True,
        "record_lineage_equivalence_passed": True,
        "physical_plan_distinctness_passed": True,
        "balanced_schedule_passed": True,
    }
    return tuple(observations), legal, integrity


def evaluate_real_governed_pipeline_transfer(
    config: RealGovernedPipelineEvaluationConfig,
    *,
    project_root: Path,
    measurement_run_dir: Path,
) -> Path:
    """Score the frozen model once; never refit or alter its threshold."""

    root = project_root.resolve()
    run_dir = measurement_run_dir.resolve()
    model_path = root / config.model_path
    development_path = root / config.development_calibration_path
    if _sha256(model_path) != config.model_sha256:
        raise ValueError("Frozen model digest changed")
    if _sha256(development_path) != config.development_calibration_sha256:
        raise ValueError("Development calibration digest changed")
    model = json.loads(model_path.read_text(encoding="utf-8"))
    measured = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    source_ids = tuple(f"{item['dataset']}-{item['month']}" for item in measured["sources"])
    profile_ids = tuple(item["profile_id"] for item in measured["profiles"])
    if source_ids != config.expected_sources:
        raise ValueError("Real source split changed")
    if profile_ids != config.expected_profiles:
        raise ValueError("Real profile split changed")
    if int(measured["row_count"]) != config.expected_row_count:
        raise ValueError("Real row count changed")
    if tuple(measured["seeds"]) != config.expected_seeds:
        raise ValueError("Real seeds changed")
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if environment.get("git_dirty") is not False:
        raise ValueError("Real measurement was not run from a clean commit")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    carryover_passed = bool(summary["gate_checks"]["no_material_carryover"])
    observations, legal, integrity = _load_real_observations(run_dir)
    integrity["systematic_carryover_passed"] = carryover_passed
    selected, predictions = _select_with_frozen_model(observations, model)
    illegal = [
        {"scenario_id": key[0], "seed": key[1], "selected_candidate_id": candidate}
        for key, candidate in selected.items()
        if candidate not in legal[key]
    ]
    metrics = _selection_metrics(
        observations,
        selected,
        practical_tie_fraction=float(model["practical_tie_fraction"]),
    )
    baselines = fixed_candidate_baselines(
        observations,
        practical_tie_fraction=float(model["practical_tie_fraction"]),
    )
    best_fixed_id, best_fixed = min(
        baselines.items(),
        key=lambda item: (
            item[1]["mean_regret_percent"],
            item[1]["p95_regret_percent"],
            item[0],
        ),
    )
    gates = {
        "minimum_oracle_set_hit_rate": (
            metrics["oracle_set_hit_rate"] >= config.minimum_oracle_set_hit_rate
        ),
        "maximum_mean_regret_percent": (
            metrics["mean_regret_percent"] <= config.maximum_mean_regret_percent
        ),
        "maximum_p95_regret_percent": (
            metrics["p95_regret_percent"] <= config.maximum_p95_regret_percent
        ),
        "maximum_regret_percent": (
            metrics["maximum_regret_percent"] <= config.maximum_regret_percent
        ),
        "minimum_selected_candidate_count": (
            len(metrics["selected_candidate_counts"]) >= config.minimum_selected_candidate_count
        ),
        "not_worse_than_best_fixed_mean": (
            not config.require_not_worse_than_best_fixed_mean
            or metrics["mean_regret_percent"] <= best_fixed["mean_regret_percent"]
        ),
        "not_worse_than_best_fixed_p95": (
            not config.require_not_worse_than_best_fixed_p95
            or metrics["p95_regret_percent"] <= best_fixed["p95_regret_percent"]
        ),
        "zero_illegal_selections": not illegal,
        "no_systematic_material_carryover": carryover_passed,
    }
    passed = all(gates.values())
    output = root / config.results_dir / run_dir.name
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "status": (
            "PASS_GOVERNED_PIPELINE_COST_MODEL_REAL_TRANSFER"
            if passed
            else "FAIL_GOVERNED_PIPELINE_COST_MODEL_REAL_TRANSFER_RETAIN"
        ),
        "model_frozen_before_measurement": True,
        "model_refit_or_threshold_change": False,
        "measurement_run_dir": str(run_dir.relative_to(root)),
        "measurement_summary_sha256": _sha256(run_dir / "summary.json"),
        "model_sha256": config.model_sha256,
        "development_calibration_sha256": config.development_calibration_sha256,
        "integrity": integrity,
        "real_transfer_metrics": metrics,
        "fixed_baselines": baselines,
        "best_fixed_candidate_id": best_fixed_id,
        "illegal_selections": illegal,
        "predictions": predictions,
        "gate_checks": gates,
        "failed_gates": sorted(name for name, passed_gate in gates.items() if not passed_gate),
        "scientific_boundary": (
            "Native BTS/NYC distributions are preserved for time, Join-key frequency, "
            "and dimension attributes. Governance labels, payload width, and dimension "
            "coverage are deterministic controlled augmentations."
        ),
    }
    _atomic_json(output / "evaluation.json", result)
    return output / "evaluation.json"
