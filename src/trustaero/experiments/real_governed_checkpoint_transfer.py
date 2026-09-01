"""Frozen real-data transfer for the governed-checkpoint optimizer.

The experiment keeps native event-time and Join-key distributions from unseen
BTS and NYC months.  A deterministic sensitive payload is added only to control
its byte width, and a reservoir sample keeps the input inside the V3.1
calibration scale.  Both legal candidates are always measured; the frozen
optimizer is scored only by the separate evaluation function at the end.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _environment,
    _git_state,
)
from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    _confidence_oracles,
    _p95,
    _sha256,
    analytic_model_from_dict,
)
from trustaero.experiments.governed_checkpoint_reversal import (
    EA1_CANDIDATE_IDS,
    POLICY_FIRST,
    QUERY_FIRST,
    _analyze,
    _digest,
    _execute_candidate,
    _feasibility,
    _profile_candidate,
    _write_measurements,
    checkpoint_orders,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import GovernedCheckpointStatistics
from trustaero.optimizer.governed_checkpoint_uncertainty import (
    CheckpointUncertaintyGuard,
    rank_uncertainty_aware_checkpoint_candidates,
)

DatasetName = Literal["bts", "nyc_tlc"]


@dataclass(frozen=True, slots=True)
class RealCheckpointSource:
    """One pre-registered, previously unused real month."""

    dataset: DatasetName
    month: str
    event_path: str
    dimension_path: str
    preparation_manifest_path: str

    @property
    def source_id(self) -> str:
        return f"{self.dataset}-{self.month}"


@dataclass(frozen=True, slots=True)
class RealCheckpointProfile:
    """Controlled governance workload placed over a real event distribution."""

    profile_id: str
    identifier_width: int
    policy_selectivity: float
    query_selectivity: float


@dataclass(frozen=True, slots=True)
class RealCheckpointTransferConfig:
    """Frozen data split, timing protocol, and inference controls."""

    results_dir: str
    sources: tuple[RealCheckpointSource, ...]
    profiles: tuple[RealCheckpointProfile, ...]
    row_count: int
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
    experiment_role: str = "frozen_real_distribution_transfer"

    def __post_init__(self) -> None:
        if not self.sources or len({item.source_id for item in self.sources}) != len(self.sources):
            raise ValueError("Real transfer sources must be nonempty and unique")
        if not self.profiles or len({item.profile_id for item in self.profiles}) != len(
            self.profiles
        ):
            raise ValueError("Real transfer profiles must be nonempty and unique")
        if self.row_count != 150_000:
            raise ValueError("V3.1 real transfer is frozen at 150000 rows")
        if len(self.seeds) < 3 or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("Real transfer requires at least three unique seeds")
        if self.candidate_ids != EA1_CANDIDATE_IDS:
            raise ValueError("Real transfer candidate pair changed")
        if self.warmup_rounds < 1 or self.repetitions_per_permutation < 15:
            raise ValueError("Real transfer timing protocol is too weak")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("Real transfer DuckDB limits are invalid")
        if self.experiment_role not in {
            "frozen_real_distribution_transfer",
            "real_mechanism_development",
            "frozen_real_optimizer_validation",
            "frozen_real_optimizer_final_holdout",
        }:
            raise ValueError("Real transfer scientific role changed")

    @property
    def measured_blocks_per_unit(self) -> int:
        return math.factorial(len(self.candidate_ids)) * self.repetitions_per_permutation


@dataclass(frozen=True, slots=True)
class RealCheckpointUnit:
    """One indivisible source-profile-seed measurement unit."""

    dataset: DatasetName
    month: str
    event_path: str
    dimension_path: str
    profile_id: str
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
        return f"{self.dataset}-{self.month}-{self.profile_id}"

    @property
    def unit_id(self) -> str:
        return f"{self.scenario_id}-s{self.seed}"


@dataclass(frozen=True, slots=True)
class RealTransferEvaluationConfig:
    """Hash-bound guard and pre-registered real-transfer acceptance gates."""

    results_dir: str
    guard_path: str
    calibration_record_path: str
    expected_guard_sha256: str
    expected_calibration_sha256: str
    minimum_confidence_family_hit_rate: float
    minimum_improvement_over_best_fixed: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_singleton_families: int
    minimum_policy_singleton_families: int
    minimum_query_singleton_families: int
    maximum_out_of_support_fallback_rate: float
    require_seed_consistency: bool
    require_clean_git: bool


def load_real_checkpoint_transfer_config(
    path: str | Path,
) -> RealCheckpointTransferConfig:
    """Load the frozen real-data measurement protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["sources"] = tuple(RealCheckpointSource(**item) for item in payload["sources"])
    payload["profiles"] = tuple(RealCheckpointProfile(**item) for item in payload["profiles"])
    payload["seeds"] = tuple(int(value) for value in payload["seeds"])
    payload["candidate_ids"] = tuple(str(value) for value in payload["candidate_ids"])
    return RealCheckpointTransferConfig(**payload)


def load_real_transfer_evaluation_config(
    path: str | Path,
) -> RealTransferEvaluationConfig:
    """Load gates which were frozen before opening real timing results."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return RealTransferEvaluationConfig(**payload)


def real_checkpoint_units(
    config: RealCheckpointTransferConfig,
) -> tuple[RealCheckpointUnit, ...]:
    """Expand the complete matrix without selecting favorable months or seeds."""

    return tuple(
        RealCheckpointUnit(
            source.dataset,
            source.month,
            source.event_path,
            source.dimension_path,
            profile.profile_id,
            config.row_count,
            profile.identifier_width,
            profile.policy_selectivity,
            profile.query_selectivity,
            seed,
        )
        for source in config.sources
        for profile in config.profiles
        for seed in config.seeds
    )


def _sql_path(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return "'" + str(path).replace("'", "''") + "'"


def _validate_sources(
    root: Path, sources: Sequence[RealCheckpointSource]
) -> dict[str, dict[str, object]]:
    """Bind each source to its preparation manifest and physical bytes."""

    records: dict[str, dict[str, object]] = {}
    for source in sources:
        manifest_path = root / source.preparation_manifest_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "PASS":
            raise ValueError(f"Prepared source did not pass: {source.source_id}")
        event_path = root / source.event_path
        dimension_path = root / source.dimension_path
        for path in (event_path, dimension_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        records[source.source_id] = {
            "manifest_path": source.preparation_manifest_path,
            "manifest_sha256": _sha256(manifest_path),
            "event_path": source.event_path,
            "event_byte_size": event_path.stat().st_size,
            "dimension_path": source.dimension_path,
            "dimension_byte_size": dimension_path.stat().st_size,
        }
    return records


def _create_real_data(connection: Any, root: Path, unit: RealCheckpointUnit) -> dict[str, int]:
    """Create an exact-size deterministic sample while retaining real order/keys."""

    for name in (
        "governed_output",
        "governance_checkpoint",
        "events",
        "dimension",
        "real_sample",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {name}")
    connection.execute("DROP VIEW IF EXISTS eligible_real_events")
    event_path = _sql_path(root, unit.event_path)
    dimension_path = _sql_path(root, unit.dimension_path)
    if unit.dataset == "bts":
        connection.execute(
            f"""
            CREATE TEMP VIEW eligible_real_events AS
            SELECT
                CAST(FlightDate AS TIMESTAMP) AS event_time,
                CAST(OriginAirportID AS BIGINT) AS join_key,
                concat_ws(
                    '|',
                    coalesce(CAST(Tail_Number AS VARCHAR), 'UNKNOWN'),
                    coalesce(CAST(Reporting_Airline AS VARCHAR), 'UNKNOWN'),
                    coalesce(CAST(Flight_Number_Reporting_Airline AS VARCHAR), '0'),
                    CAST(FlightDate AS VARCHAR),
                    coalesce(CAST(Origin AS VARCHAR), 'UNKNOWN'),
                    coalesce(CAST(Dest AS VARCHAR), 'UNKNOWN')
                ) AS native_token
            FROM read_parquet({event_path})
            WHERE FlightDate IS NOT NULL AND OriginAirportID IS NOT NULL
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE dimension AS
            SELECT
                CAST(airport_id AS BIGINT) AS dimension_key,
                CAST(hash(airport_code, city_name, state_code) % 97 AS BIGINT) AS marker
            FROM read_parquet({dimension_path})
            """
        )
    elif unit.dataset == "nyc_tlc":
        connection.execute(
            f"""
            CREATE TEMP VIEW eligible_real_events AS
            SELECT
                CAST(tpep_pickup_datetime AS TIMESTAMP) AS event_time,
                CAST(PULocationID AS BIGINT) AS join_key,
                concat_ws(
                    '|',
                    coalesce(CAST(VendorID AS VARCHAR), '0'),
                    CAST(tpep_pickup_datetime AS VARCHAR),
                    CAST(tpep_dropoff_datetime AS VARCHAR),
                    coalesce(CAST(PULocationID AS VARCHAR), '0'),
                    coalesce(CAST(DOLocationID AS VARCHAR), '0'),
                    coalesce(CAST(total_amount AS VARCHAR), '0')
                ) AS native_token
            FROM read_parquet({event_path})
            WHERE
                tpep_pickup_datetime IS NOT NULL
                AND PULocationID IS NOT NULL
                AND PULocationID > 0
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE dimension AS
            SELECT
                CAST(LocationID AS BIGINT) AS dimension_key,
                CAST(hash(Borough, Zone, service_zone) % 97 AS BIGINT) AS marker
            FROM read_parquet({dimension_path})
            """
        )
    else:  # pragma: no cover - dataclass type and config validation constrain this
        raise ValueError(f"Unknown real dataset: {unit.dataset}")

    connection.execute(
        f"""
        CREATE TEMP TABLE real_sample AS
        SELECT *
        FROM eligible_real_events
        USING SAMPLE reservoir({unit.row_count} ROWS) REPEATABLE({unit.seed})
        """
    )
    sampled_rows = int(connection.execute("SELECT count(*) FROM real_sample").fetchone()[0])
    if sampled_rows != unit.row_count:
        raise ValueError(
            f"Real source is too small for frozen sample: {unit.unit_id}={sampled_rows}"
        )
    blocks = math.ceil(unit.identifier_width / 32)
    connection.execute(
        f"""
        CREATE TEMP TABLE events AS
        WITH ranked AS (
            SELECT
                row_number() OVER (
                    ORDER BY event_time, join_key, native_token
                ) - 1 AS row_id,
                event_time,
                join_key,
                native_token
            FROM real_sample
        )
        SELECT
            CAST(row_id AS BIGINT) AS row_id,
            left(
                repeat(md5(native_token || ':' || CAST(row_id AS VARCHAR)), {blocks}),
                {unit.identifier_width}
            ) AS sensitive_value,
            CAST(join_key AS BIGINT) AS join_key,
            CAST(floor(row_id * 10000.0 / {unit.row_count}) AS INTEGER) AS query_bucket
        FROM ranked
        """
    )
    observed = connection.execute(
        "SELECT count(*), min(length(sensitive_value)), max(length(sensitive_value)) FROM events"
    ).fetchone()
    if observed != (unit.row_count, unit.identifier_width, unit.identifier_width):
        raise ValueError(f"Real transfer width validation failed: {unit.unit_id}")
    policy_rows, query_rows, result_rows = connection.execute(
        f"""
        SELECT
            count(*) FILTER (
                WHERE hash(events.sensitive_value) % 10000 < {unit.policy_cutoff}
            ),
            count(*) FILTER (WHERE events.query_bucket < {unit.query_cutoff}),
            count(*) FILTER (
                WHERE hash(events.sensitive_value) % 10000 < {unit.policy_cutoff}
                  AND events.query_bucket < {unit.query_cutoff}
                  AND dimension.dimension_key IS NOT NULL
            )
        FROM events
        LEFT JOIN dimension ON events.join_key = dimension.dimension_key
        """
    ).fetchone()
    return {
        "input_rows": unit.row_count,
        "policy_rows": int(policy_rows),
        "query_rows": int(query_rows),
        "result_rows": int(result_rows),
    }


def _run_unit(
    config: RealCheckpointTransferConfig,
    unit: RealCheckpointUnit,
    root: Path,
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
        connection.execute(
            "SET temp_directory = '" + str(temp_dir.resolve()).replace("'", "''") + "'"
        )
        actual = _create_real_data(connection, root, unit)
        feasibility = _feasibility(actual)
        profiles = {
            candidate_id: _profile_candidate(
                connection,
                cast(Any, unit),
                candidate_id,
                output_dir / "plans" / unit.unit_id,
            )
            for candidate_id in config.candidate_ids
        }
        if len({str(profile["combined_fingerprint"]) for profile in profiles.values()}) != 2:
            raise ValueError("Real transfer physical candidate plans are not distinct")
        order_seed = (
            config.order_seed
            + unit.seed
            + int.from_bytes(hashlib.sha256(unit.scenario_id.encode()).digest()[:4], "big")
        )
        for repeat_index, order in enumerate(
            checkpoint_orders(config.candidate_ids, config.warmup_rounds, seed=order_seed)
        ):
            for position, candidate_id in enumerate(order):
                _execute_candidate(
                    connection,
                    cast(Any, unit),
                    candidate_id,
                    repeat_index=repeat_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )
        measurements: list[dict[str, object]] = []
        orders = checkpoint_orders(
            config.candidate_ids,
            config.repetitions_per_permutation,
            seed=order_seed + 1,
        )
        for block_index, order in enumerate(orders):
            block_rows = [
                _execute_candidate(
                    connection,
                    cast(Any, unit),
                    candidate_id,
                    repeat_index=block_index,
                    order_position=position,
                    permutation_id=">".join(order),
                )
                for position, candidate_id in enumerate(order)
            ]
            if len({str(row["result_digest"]) for row in block_rows}) != 1:
                raise ValueError("Real transfer candidates returned different results")
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
            "field_provenance": {
                "event_time": "native",
                "join_key": "native",
                "sensitive_payload": "deterministic_controlled_from_native_token",
                "query_bucket": "rank_of_native_event_time",
                "policy_bucket": "hash_of_controlled_sensitive_payload",
            },
        }
    finally:
        connection.close()


def run_real_governed_checkpoint_transfer(
    config: RealCheckpointTransferConfig,
    *,
    project_root: Path,
    resume_run_id: str | None = None,
    progress_callback: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume the frozen real-distribution measurement matrix."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Real transfer requires a clean Git commit")
    source_records = _validate_sources(root, config.sources)
    config_payload = asdict(config)
    config_digest = _digest(config_payload)
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    if resume_run_id:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        environment = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))
        if checkpoint.get("config_digest") != config_digest:
            raise ValueError("Real transfer resume configuration changed")
        if environment.get("commit_hash") != commit:
            raise ValueError("Real transfer resume commit changed")
    else:
        checkpoint = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_units": [],
            "status": "running",
        }
        _atomic_json(output_dir / "config.json", config_payload)
        environment = _environment(commit, dirty, cast(Any, config))
        environment["source_records"] = source_records
        environment["cache_protocol"] = "warm_same_unit_connection"
        environment["gpu_acceleration"] = False
        _atomic_json(output_dir / "environment.json", environment)
        _atomic_json(checkpoint_path, checkpoint)
        _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})

    units = real_checkpoint_units(config)
    completed = set(cast(list[str], checkpoint["completed_units"]))
    remaining = tuple(unit for unit in units if unit.unit_id not in completed)
    started = time.perf_counter()
    total_blocks = len(remaining) * config.measured_blocks_per_unit
    blocks_done = 0
    for unit in remaining:
        payload = _run_unit(
            config,
            unit,
            root,
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
    summary = _analyze(output_dir, cast(Any, config))
    is_development = config.experiment_role == "real_mechanism_development"
    is_validation = config.experiment_role == "frozen_real_optimizer_validation"
    is_final_holdout = config.experiment_role == "frozen_real_optimizer_final_holdout"
    summary.update(
        {
            "status": (
                "PASS_EA1_REAL_MECHANISM_DEVELOPMENT_INTEGRITY"
                if is_development
                else (
                    "PASS_EA1_REAL_OPTIMIZER_FINAL_HOLDOUT_MEASUREMENT_INTEGRITY"
                    if is_final_holdout
                    else (
                        "PASS_EA1_REAL_OPTIMIZER_VALIDATION_MEASUREMENT_INTEGRITY"
                        if is_validation
                        else "PASS_EA1_REAL_TRANSFER_MEASUREMENT_INTEGRITY"
                    )
                )
            ),
            "dataset_count": len({item.dataset for item in config.sources}),
            "source_month_count": len(config.sources),
            "unit_count": len(units),
            "native_distribution_preserved": [
                "event_time",
                "join_key",
                "dimension_relation",
            ],
            "controlled_fields": [
                "sensitive_payload_width",
                "policy_selectivity",
                "query_selectivity_via_native_time_rank",
            ],
            "paper_optimizer_performance_claim_authorized": False,
            "scientific_boundary": (
                "This is a pre-registered 150K-row real-data mechanism-development "
                "matrix. It may be used to calibrate a future optimizer, but it is "
                "not optimizer holdout evidence."
                if is_development
                else (
                    "This is the untouched-month V4.1 final holdout measurement. "
                    "The optimizer and all gates were frozen before these timings; "
                    "only the separate evaluator may authorize a paper claim."
                    if is_final_holdout
                    else (
                        "This is a frozen real-month V4 validation measurement. It may "
                        "select whether V4 advances to final holdout, but it is not the "
                        "final paper performance result."
                        if is_validation
                        else "This is a frozen 150K-row real-distribution transfer "
                        "measurement. It does not test full-month scale transfer, and "
                        "optimizer claims require the separately frozen V3.1 evaluator."
                    )
                )
            ),
        }
    )
    _atomic_json(output_dir / "summary.json", summary)
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = datetime.now(UTC).isoformat()
    _atomic_json(checkpoint_path, checkpoint)
    return output_dir


def _load_guard(
    config: RealTransferEvaluationConfig, root: Path
) -> tuple[CheckpointUncertaintyGuard, dict[str, str]]:
    guard_path = root / config.guard_path
    calibration_path = root / config.calibration_record_path
    hashes = {
        "guard": _sha256(guard_path),
        "development_calibration": _sha256(calibration_path),
    }
    if hashes != {
        "guard": config.expected_guard_sha256,
        "development_calibration": config.expected_calibration_sha256,
    }:
        raise ValueError("Frozen V3.1 real-transfer binding changed")
    payload = json.loads(guard_path.read_text(encoding="utf-8"))
    return (
        CheckpointUncertaintyGuard(
            base_model=analytic_model_from_dict(payload["base_model"]),
            query_margin_error_upper_ms=float(payload["query_margin_error_upper_ms"]),
            coverage=float(payload["coverage"]),
            calibration_family_count=int(payload["calibration_family_count"]),
            calibration_method=str(payload["calibration_method"]),
        ),
        hashes,
    )


def _real_statistics_and_medians(
    run_dir: Path,
) -> tuple[
    dict[tuple[str, int], GovernedCheckpointStatistics],
    dict[tuple[str, int, str], float],
]:
    statistics_by_key: dict[tuple[str, int], GovernedCheckpointStatistics] = {}
    for path in sorted((run_dir / "units").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        unit = cast(dict[str, Any], payload["unit"])
        actual = cast(dict[str, Any], payload["actual_cardinalities"])
        scenario_id = str(payload["unit_id"]).rsplit("-s", 1)[0]
        seed = int(unit["seed"])
        statistics_by_key[(scenario_id, seed)] = GovernedCheckpointStatistics(
            input_rows=int(unit["row_count"]),
            sensitive_width_bytes=float(unit["identifier_width"]),
            estimated_policy_rows=int(actual["policy_rows"]),
            estimated_query_rows=int(actual["query_rows"]),
            estimated_result_rows=int(actual["result_rows"]),
            statistic_provenance="catalog_exact_controlled",
        )
    samples: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            samples[(row["scenario_id"], int(row["seed"]), row["candidate_id"])].append(
                float(row["latency_ms"])
            )
    medians = {key: statistics.median(values) for key, values in samples.items()}
    return statistics_by_key, medians


def evaluate_real_governed_checkpoint_transfer(
    config: RealTransferEvaluationConfig,
    *,
    source_run_dir: str | Path,
    project_root: Path,
) -> Path:
    """Score the frozen V3.1 guard once against real-distribution measurements."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Real transfer evaluation requires a clean Git commit")
    run_dir = Path(source_run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_EA1_REAL_TRANSFER_MEASUREMENT_INTEGRITY":
        raise ValueError("Real transfer measurement integrity did not pass")
    if environment.get("git_dirty") is not False:
        raise ValueError("Real transfer measurement used a dirty worktree")
    guard, frozen_hashes = _load_guard(config, root)
    oracles = _confidence_oracles(summary)
    statistics_by_key, medians = _real_statistics_and_medians(run_dir)

    permissive = GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None)
    strict = GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0)
    decisions: list[dict[str, object]] = []
    strict_violations = 0
    for (scenario_id, seed), planner_statistics in sorted(statistics_by_key.items()):
        ranking = rank_uncertainty_aware_checkpoint_candidates(
            planner_statistics, permissive, guard
        )
        selected = ranking.selected_candidate_id
        if selected is None:
            raise ValueError("V3.1 rejected every permissive real candidate")
        strict_ranking = rank_uncertainty_aware_checkpoint_candidates(
            planner_statistics, strict, guard
        )
        strict_violations += strict_ranking.selected_candidate_id != POLICY_FIRST
        actual = {
            candidate_id: medians[(scenario_id, seed, candidate_id)]
            for candidate_id in EA1_CANDIDATE_IDS
        }
        best = min(actual.values())
        decisions.append(
            {
                "scenario_id": scenario_id,
                "seed": seed,
                "selected_candidate_id": selected,
                "reason_code": ranking.reason_code,
                "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                "confidence_oracle_hit": selected in oracles[scenario_id],
                "diagnostic_median_regret_percent": (actual[selected] / best - 1.0) * 100.0,
            }
        )

    family_selections: dict[str, set[str]] = defaultdict(set)
    for row in decisions:
        family_selections[str(row["scenario_id"])].add(str(row["selected_candidate_id"]))
    seed_consistency = all(len(values) == 1 for values in family_selections.values())
    singleton_oracles = {
        scenario_id: candidates
        for scenario_id, candidates in oracles.items()
        if len(candidates) == 1
    }
    optimizer_hits = sum(
        next(iter(family_selections[scenario_id])) in candidates
        for scenario_id, candidates in singleton_oracles.items()
    )
    fixed_hits = {
        candidate_id: sum(candidate_id in candidates for candidates in singleton_oracles.values())
        for candidate_id in EA1_CANDIDATE_IDS
    }
    singleton_count = len(singleton_oracles)
    optimizer_rate = optimizer_hits / singleton_count if singleton_count else 0.0
    fixed_rates = {
        key: value / singleton_count if singleton_count else 0.0
        for key, value in fixed_hits.items()
    }
    best_fixed = max(fixed_rates.values(), default=0.0)
    regrets = [float(cast(float, row["diagnostic_median_regret_percent"])) for row in decisions]
    out_of_support = sum(
        row["reason_code"] == "GOVERNED_CHECKPOINT_OUT_OF_SUPPORT_SAFE_FALLBACK"
        for row in decisions
    )
    policy_singletons = sum(
        candidates == (POLICY_FIRST,) for candidates in singleton_oracles.values()
    )
    query_singletons = sum(
        candidates == (QUERY_FIRST,) for candidates in singleton_oracles.values()
    )
    mean_regret = statistics.mean(regrets)
    p95_regret = _p95(regrets)
    maximum_regret = max(regrets)
    out_of_support_rate = out_of_support / len(decisions)
    metrics: dict[str, object] = {
        "confidence_family_hit_rate": optimizer_rate,
        "fixed_policy_confidence_hit_rate": fixed_rates.get(POLICY_FIRST, 0.0),
        "fixed_query_confidence_hit_rate": fixed_rates.get(QUERY_FIRST, 0.0),
        "best_fixed_confidence_hit_rate": best_fixed,
        "improvement_over_best_fixed": optimizer_rate - best_fixed,
        "mean_regret_percent": mean_regret,
        "p95_regret_percent": p95_regret,
        "max_regret_percent": maximum_regret,
        "singleton_family_count": singleton_count,
        "policy_singleton_family_count": policy_singletons,
        "query_singleton_family_count": query_singletons,
        "out_of_support_fallback_rate": out_of_support_rate,
        "seed_consistency": seed_consistency,
        "strict_policy_illegal_selection_count": strict_violations,
        "reason_counts": dict(Counter(str(row["reason_code"]) for row in decisions)),
    }
    gates = {
        "minimum_confidence_family_hit_rate": optimizer_rate
        >= config.minimum_confidence_family_hit_rate,
        "minimum_improvement_over_best_fixed": optimizer_rate - best_fixed
        >= config.minimum_improvement_over_best_fixed,
        "maximum_mean_regret_percent": mean_regret <= config.maximum_mean_regret_percent,
        "maximum_p95_regret_percent": p95_regret <= config.maximum_p95_regret_percent,
        "maximum_regret_percent": maximum_regret <= config.maximum_regret_percent,
        "minimum_singleton_families": singleton_count >= config.minimum_singleton_families,
        "minimum_policy_singleton_families": policy_singletons
        >= config.minimum_policy_singleton_families,
        "minimum_query_singleton_families": query_singletons
        >= config.minimum_query_singleton_families,
        "maximum_out_of_support_fallback_rate": out_of_support_rate
        <= config.maximum_out_of_support_fallback_rate,
        "seed_consistency": seed_consistency or not config.require_seed_consistency,
        "governance_legality": strict_violations == 0,
    }
    passed = all(gates.values())
    result = {
        "status": (
            "PASS_EA1_V31_REAL_DISTRIBUTION_TRANSFER"
            if passed
            else "FAIL_EA1_V31_REAL_DISTRIBUTION_TRANSFER_RETAIN"
        ),
        "metrics": metrics,
        "gates": gates,
        "decisions": decisions,
        "frozen_hashes": frozen_hashes,
        "measurement_run": str(run_dir.relative_to(root)),
        "measurement_commit_hash": environment["commit_hash"],
        "evaluation_commit_hash": commit,
        "evaluation_git_dirty": dirty,
        "real_distribution_transfer_claim_authorized": passed,
        "full_month_scale_claim_authorized": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "Passing authorizes a 150K-row real-distribution transfer claim on "
            "pre-registered unseen months. Full-month scale and final paper claims "
            "still require separate frozen experiments."
        ),
    }
    output_root = root / config.results_dir
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "evaluation.json", result)
    _atomic_json(output_root / "latest_run.json", {"run_id": run_id})
    return output_dir
