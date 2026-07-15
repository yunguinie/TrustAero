"""Phase 2C stability runner with balanced scheduling and safe checkpoints.

The runner treats one ``scenario x row_count x data_seed`` combination as an
atomic unit. Completed units are never repeated on resume; an interrupted unit
is rerun in full so partial timing samples cannot silently enter paper tables.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from trustaero.execution import capture_source_lineage, observe_duckdb_plan
from trustaero.experiments.phase2a import build_phase2_experiment_plan
from trustaero.experiments.physical_sql import (
    compile_phase2_strategy,
    supported_phase2_materialization_targets,
)
from trustaero.experiments.synthetic import (
    SyntheticDataConfig,
    generate_synthetic_workload,
)
from trustaero.ir.models import ApprovedPhysicalPlan
from trustaero.planner import generate_duckdb_candidates


@dataclass(frozen=True)
class Phase2CScenario:
    """One controlled distribution; scale and seed form separate axes."""

    scenario_id: str
    temporal_selectivity: float
    spatial_selectivity: float
    policy_selectivity: float
    join_match_rate: float
    hot_key_fraction: float
    identifier_width: int = 18

    def __post_init__(self) -> None:
        if not self.scenario_id:
            raise ValueError("scenario_id cannot be empty")
        for field_name in (
            "temporal_selectivity",
            "spatial_selectivity",
            "policy_selectivity",
            "join_match_rate",
            "hot_key_fraction",
        ):
            value = float(getattr(self, field_name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be in [0, 1]")
        if self.hot_key_fraction > self.join_match_rate:
            raise ValueError("hot_key_fraction cannot exceed join_match_rate")
        if not 18 <= self.identifier_width <= 4096:
            raise ValueError("identifier_width must be between 18 and 4096 characters")


@dataclass(frozen=True)
class Phase2CConfig:
    """Frozen experiment protocol for stability and scale measurements."""

    results_dir: str
    scenarios: tuple[Phase2CScenario, ...]
    row_counts: tuple[int, ...]
    seeds: tuple[int, ...]
    warmup_runs: int = 5
    measured_runs: int = 30
    duckdb_threads: int = 4
    duckdb_memory_limit_mb: int = 4096
    order_seed: int = 20260715
    tie_threshold_fraction: float = 0.03
    source_lineage: bool = True
    require_clean_git: bool = False
    materialization_targets: tuple[str, ...] = (
        "op-temporal",
        "op-spatial",
        "op-policy",
        "op-event-project",
    )
    filter_orders: tuple[tuple[str, ...], ...] = ()
    mask_event_id: bool = False
    operator_placements: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.scenarios or not self.row_counts or not self.seeds:
            raise ValueError("Phase 2C scenarios, row_counts, and seeds cannot be empty")
        if len({item.scenario_id for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("Phase 2C scenario IDs must be unique")
        if any(value < 1 for value in self.row_counts):
            raise ValueError("row_counts must be positive")
        if any(value < 0 for value in self.seeds):
            raise ValueError("seeds cannot be negative")
        if self.warmup_runs < 0 or self.measured_runs < 1:
            raise ValueError("warmup_runs must be nonnegative and measured_runs positive")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("DuckDB resource limits are invalid")
        if not 0.0 <= self.tie_threshold_fraction < 1.0:
            raise ValueError("tie_threshold_fraction must be in [0, 1)")
        if (
            not self.materialization_targets
            and not self.filter_orders
            and not self.operator_placements
        ):
            raise ValueError("at least one non-fused physical candidate is required")
        if self.operator_placements and not self.mask_event_id:
            raise ValueError("operator placements require the controlled Mask obligation")


@dataclass(frozen=True)
class Phase2CMeasurement:
    """One measured candidate execution; warmups are deliberately excluded."""

    run_id: str
    commit_hash: str
    unit_id: str
    scenario_id: str
    row_count: int
    data_seed: int
    repeat_index: int
    order_position: int
    strategy_id: str
    approved_physical_plan_id: str
    physical_plan_fingerprint: str
    end_to_end_latency_ms: float
    lineage_latency_ms: float
    governed_latency_ms: float
    output_row_count: int
    result_digest: str


def balanced_candidate_orders(
    strategy_ids: tuple[str, ...],
    round_count: int,
    *,
    offset_seed: int,
) -> tuple[tuple[str, ...], ...]:
    """Rotate candidates so execution positions are deterministically balanced."""

    if not strategy_ids or round_count < 0:
        raise ValueError("balanced orders require candidates and nonnegative rounds")
    offset = offset_seed % len(strategy_ids)
    values = list(strategy_ids)
    orders: list[tuple[str, ...]] = []
    for round_index in range(round_count):
        rotation = (offset + round_index) % len(values)
        orders.append(tuple(values[rotation:] + values[:rotation]))
    return tuple(orders)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip()


def _git_dirty(root: Path) -> bool:
    """Return whether tracked or untracked files could affect a paper run."""

    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "status", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(completed.stdout.strip())


def _digest(value: object) -> str:
    encoded = json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, payload: object) -> None:
    """Replace a JSON artifact atomically so interruption cannot truncate it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _environment(
    commit_hash: str,
    git_dirty: bool,
    config: Phase2CConfig,
) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for package in ("trustaero", "duckdb", "pydantic"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "unknown"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "commit_hash": commit_hash,
        "git_dirty": git_dirty,
        "packages": packages,
        "duckdb": {
            "threads": config.duckdb_threads,
            "memory_limit_mb": config.duckdb_memory_limit_mb,
            "gpu_acceleration": False,
        },
    }


def _unit_id(scenario_id: str, row_count: int, seed: int) -> str:
    return f"{scenario_id}-n{row_count}-seed{seed}"


def _stable_offset(unit_id: str, order_seed: int) -> int:
    encoded = hashlib.sha256(f"{unit_id}:{order_seed}".encode()).digest()
    return int.from_bytes(encoded[:8], "big")


def _percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def _stage_statistics(connection: Any) -> dict[str, int]:
    """Measure logical-stage cardinalities outside the timed candidate loop."""

    row = connection.execute(
        """
        SELECT
          COUNT(*) FILTER (
            WHERE event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
              AND event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
          ) AS after_temporal,
          COUNT(*) FILTER (
            WHERE event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
              AND event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
              AND 111.045 * sqrt(
                power(latitude - 40.0, 2)
                + power((longitude - 116.3) * cos(radians(40.0)), 2)
              ) <= 20.0
          ) AS after_spatial,
          COUNT(*) FILTER (
            WHERE event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
              AND event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
              AND 111.045 * sqrt(
                power(latitude - 40.0, 2)
                + power((longitude - 116.3) * cos(radians(40.0)), 2)
              ) <= 20.0
              AND policy_allowed
          ) AS after_policy
        FROM synthetic_events
        """
    ).fetchone()
    join_row = connection.execute(
        """
        SELECT COUNT(*)
        FROM synthetic_events AS events
        INNER JOIN severity_dim AS dimension
          ON events.join_key = dimension.dimension_key
        WHERE events.event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
          AND events.event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
          AND 111.045 * sqrt(
            power(events.latitude - 40.0, 2)
            + power((events.longitude - 116.3) * cos(radians(40.0)), 2)
          ) <= 20.0
          AND events.policy_allowed
        """
    ).fetchone()
    if row is None or join_row is None:
        raise RuntimeError("DuckDB did not return Phase 2C stage cardinalities")
    return {
        "after_temporal_rows": int(row[0]),
        "after_spatial_rows": int(row[1]),
        "after_policy_rows": int(row[2]),
        "after_join_rows": int(join_row[0]),
    }


class _ProgressReporter:
    """Console plus JSON progress with an ETA based on completed unit fractions."""

    def __init__(
        self,
        *,
        output_dir: Path,
        latest_path: Path,
        run_id: str,
        show_console: bool,
    ) -> None:
        self.output_dir = output_dir
        self.latest_path = latest_path
        self.run_id = run_id
        self.show_console = show_console
        self.started = time.monotonic()
        self.last_file_write = 0.0

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, round(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def update(
        self,
        *,
        unit_index: int,
        unit_count: int,
        unit_id: str,
        round_index: int,
        round_count: int,
        candidate_position: int,
        candidate_count: int,
        strategy_id: str,
        status: str,
        force_file: bool = False,
    ) -> None:
        within_unit = (round_index + candidate_position / max(1, candidate_count)) / max(
            1, round_count
        )
        fraction = min(1.0, (unit_index + within_unit) / max(1, unit_count))
        elapsed = time.monotonic() - self.started
        eta = elapsed * (1.0 - fraction) / fraction if fraction > 0 else 0.0
        payload = {
            "run_id": self.run_id,
            "status": status,
            "unit_id": unit_id,
            "unit_index": unit_index + 1,
            "unit_count": unit_count,
            "round_index": round_index + 1,
            "round_count": round_count,
            "candidate_position": candidate_position,
            "candidate_count": candidate_count,
            "strategy_id": strategy_id,
            "percent": round(fraction * 100.0, 2),
            "elapsed": self._duration(elapsed),
            "eta": self._duration(eta),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if self.show_console:
            filled = round(28 * fraction)
            bar = "#" * filled + "-" * (28 - filled)
            print(
                f"\rPhase 2C [{bar}] {fraction * 100:6.2f}% "
                f"{unit_id} | {strategy_id} | ETA {self._duration(eta)}",
                end="",
                flush=True,
            )
        now = time.monotonic()
        if force_file or now - self.last_file_write >= 0.5:
            _write_json_atomic(self.output_dir / "progress.json", payload)
            _write_json_atomic(self.latest_path, payload)
            self.last_file_write = now

    def finish(self, unit_count: int) -> None:
        self.update(
            unit_index=unit_count,
            unit_count=unit_count,
            unit_id="complete",
            round_index=1,
            round_count=1,
            candidate_position=1,
            candidate_count=1,
            strategy_id="complete",
            status="complete",
            force_file=True,
        )
        if self.show_console:
            print()


def _execute_candidate(
    connection: Any,
    sql: str,
    *,
    logical_plan: Any,
    execution_id: str,
) -> tuple[float, float, int, str]:
    started = time.perf_counter()
    rows = tuple(tuple(row) for row in connection.execute(sql).fetchall())
    latency_ms = (time.perf_counter() - started) * 1000.0
    result_digest = _digest(rows)
    lineage = capture_source_lineage(
        logical_plan,
        execution_id=execution_id,
        result_id=result_digest,
    )
    return latency_ms, lineage.latency_ms, len(rows), result_digest


def _candidate_representatives(
    connection: Any,
    candidates: tuple[ApprovedPhysicalPlan, ...],
    plan_dir: Path,
    logical_plan: Any,
) -> tuple[
    tuple[ApprovedPhysicalPlan, ...],
    dict[str, str],
    dict[str, list[str]],
    dict[str, dict[str, Any]],
]:
    """Execute analyzed discovery once and retain one actual tree per group.

    Plain EXPLAIN and EXPLAIN ANALYZE have different wrapper nodes in DuckDB
    1.5. Grouping on the analyzed form avoids comparing incompatible shapes and
    also captures memory and spill metrics before balanced timing begins.
    """

    plan_dir.mkdir(parents=True, exist_ok=True)
    fingerprint_by_strategy: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    profile_by_strategy: dict[str, dict[str, Any]] = {}
    by_strategy = {item.strategy.strategy_id: item for item in candidates}
    for candidate in candidates:
        strategy_id = candidate.strategy.strategy_id
        observation = observe_duckdb_plan(
            connection,
            compile_phase2_strategy(candidate, logical_plan),
            analyze=True,
        )
        fingerprint_by_strategy[strategy_id] = observation.fingerprint
        groups.setdefault(observation.fingerprint, []).append(strategy_id)
        profile_by_strategy[strategy_id] = {
            "profile_latency_ms": observation.profile_latency_ms,
            "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
            "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
            "total_memory_allocated_bytes": observation.total_memory_allocated_bytes,
            "max_intermediate_cardinality": observation.max_intermediate_cardinality,
            "total_rows_scanned": sum(observation.rows_scanned),
            "physical_operator_names": list(observation.operator_names),
            "materialization_operator_time_ms": sum(
                timing
                for name, timing in zip(
                    observation.operator_names,
                    observation.operator_timings_ms,
                    strict=True,
                )
                if name in {"CTE", "CTE_SCAN"}
            ),
            "has_mask": any(
                operator.operator_type == "Mask" for operator in candidate.physical_operators
            ),
            "mask_before_join": candidate.strategy.execution_mode == "governance_placed",
        }
        (plan_dir / f"{strategy_id}-discovery-analyze.json").write_text(
            observation.plan_json + "\n", encoding="utf-8"
        )
    representatives = tuple(by_strategy[strategies[0]] for strategies in groups.values())
    representative_profiles = {
        candidate.strategy.strategy_id: profile_by_strategy[candidate.strategy.strategy_id]
        for candidate in representatives
    }
    return representatives, fingerprint_by_strategy, groups, representative_profiles


def _strategy_summaries(
    measurements: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    stage_stats: dict[str, int],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for measurement in measurements:
        grouped.setdefault(str(measurement["strategy_id"]), []).append(measurement)
    summaries: list[dict[str, Any]] = []
    for strategy_id, rows in grouped.items():
        governed = [float(item["governed_latency_ms"]) for item in rows]
        end_to_end = [float(item["end_to_end_latency_ms"]) for item in rows]
        lineage = [float(item["lineage_latency_ms"]) for item in rows]
        profile = profiles[strategy_id]
        summaries.append(
            {
                "strategy_id": strategy_id,
                "approved_physical_plan_id": rows[0]["approved_physical_plan_id"],
                "physical_plan_fingerprint": rows[0]["physical_plan_fingerprint"],
                "runs": len(rows),
                "median_end_to_end_latency_ms": statistics.median(end_to_end),
                "p95_end_to_end_latency_ms": _percentile_95(end_to_end),
                "median_lineage_latency_ms": statistics.median(lineage),
                "median_governed_latency_ms": statistics.median(governed),
                "p95_governed_latency_ms": _percentile_95(governed),
                "profile_latency_ms": profile["profile_latency_ms"],
                "peak_buffer_memory_bytes": profile["peak_buffer_memory_bytes"],
                "peak_temp_directory_bytes": profile["peak_temp_directory_bytes"],
                "total_memory_allocated_bytes": profile["total_memory_allocated_bytes"],
                "max_intermediate_cardinality": profile["max_intermediate_cardinality"],
                "materialization_operator_time_ms": profile["materialization_operator_time_ms"],
                "raw_sensitive_rows_exposed_to_join": (
                    0
                    if not profile["has_mask"] or profile["mask_before_join"]
                    else stage_stats["after_policy_rows"]
                ),
                "mask_rows_processed": (
                    0
                    if not profile["has_mask"]
                    else (
                        stage_stats["after_policy_rows"]
                        if profile["mask_before_join"]
                        else stage_stats["after_join_rows"]
                    )
                ),
                **stage_stats,
            }
        )
    return summaries


def _run_unit(
    connection: Any,
    *,
    config: Phase2CConfig,
    scenario: Phase2CScenario,
    row_count: int,
    data_seed: int,
    run_id: str,
    commit_hash: str,
    logical_plan: Any,
    candidates: tuple[ApprovedPhysicalPlan, ...],
    unit_index: int,
    unit_count: int,
    output_dir: Path,
    reporter: _ProgressReporter,
) -> dict[str, Any]:
    unit_id = _unit_id(scenario.scenario_id, row_count, data_seed)
    workload = SyntheticDataConfig(
        workload_id=unit_id,
        row_count=row_count,
        temporal_selectivity=scenario.temporal_selectivity,
        spatial_selectivity=scenario.spatial_selectivity,
        policy_selectivity=scenario.policy_selectivity,
        join_match_rate=scenario.join_match_rate,
        hot_key_fraction=scenario.hot_key_fraction,
        identifier_width=scenario.identifier_width,
        seed=data_seed,
    )
    realized = generate_synthetic_workload(connection, workload)
    stage_stats = _stage_statistics(connection)
    plan_dir = output_dir / "plans" / unit_id
    (
        representatives,
        fingerprints,
        fingerprint_groups,
        profiles,
    ) = _candidate_representatives(connection, candidates, plan_dir, logical_plan)
    by_strategy = {item.strategy.strategy_id: item for item in representatives}
    strategy_ids = tuple(by_strategy)
    total_rounds = config.warmup_runs + config.measured_runs
    orders = balanced_candidate_orders(
        strategy_ids,
        total_rounds,
        offset_seed=_stable_offset(unit_id, config.order_seed),
    )
    measurements: list[dict[str, Any]] = []
    expected_digest: str | None = None
    expected_count: int | None = None
    for round_index, order in enumerate(orders):
        for position, strategy_id in enumerate(order, start=1):
            candidate = by_strategy[strategy_id]
            latency_ms, lineage_ms, output_count, result_digest = _execute_candidate(
                connection,
                compile_phase2_strategy(candidate, logical_plan),
                logical_plan=logical_plan,
                execution_id=f"{run_id}:{unit_id}:{round_index}:{strategy_id}",
            )
            if expected_digest is None:
                expected_digest = result_digest
                expected_count = output_count
            elif result_digest != expected_digest or output_count != expected_count:
                raise RuntimeError(f"{unit_id} candidate results are not equivalent")
            if round_index >= config.warmup_runs:
                measurement = Phase2CMeasurement(
                    run_id=run_id,
                    commit_hash=commit_hash,
                    unit_id=unit_id,
                    scenario_id=scenario.scenario_id,
                    row_count=row_count,
                    data_seed=data_seed,
                    repeat_index=round_index - config.warmup_runs,
                    order_position=position,
                    strategy_id=strategy_id,
                    approved_physical_plan_id=candidate.physical_plan_id,
                    physical_plan_fingerprint=fingerprints[strategy_id],
                    end_to_end_latency_ms=latency_ms,
                    lineage_latency_ms=lineage_ms,
                    governed_latency_ms=latency_ms + lineage_ms,
                    output_row_count=output_count,
                    result_digest=result_digest,
                )
                measurements.append(asdict(measurement))
            reporter.update(
                unit_index=unit_index,
                unit_count=unit_count,
                unit_id=unit_id,
                round_index=round_index,
                round_count=total_rounds,
                candidate_position=position,
                candidate_count=len(order),
                strategy_id=strategy_id,
                status="running",
            )

    strategy_summaries = _strategy_summaries(measurements, profiles, stage_stats)
    ranked = sorted(strategy_summaries, key=lambda item: item["median_governed_latency_ms"])
    gap = (
        float(ranked[1]["median_governed_latency_ms"])
        - float(ranked[0]["median_governed_latency_ms"])
    ) / float(ranked[0]["median_governed_latency_ms"])
    winner = "tie" if gap < config.tie_threshold_fraction else str(ranked[0]["strategy_id"])
    return {
        "unit_id": unit_id,
        "scenario": asdict(scenario),
        "row_count": row_count,
        "data_seed": data_seed,
        "realized_statistics": asdict(realized),
        "stage_statistics": stage_stats,
        "nominal_candidate_count": len(candidates),
        "unique_physical_plan_count": len(representatives),
        "fingerprint_groups": list(fingerprint_groups.values()),
        "result_equivalent": True,
        "output_row_count": expected_count,
        "result_digest": expected_digest,
        "observed_winner": winner,
        "winner_gap_fraction": gap,
        "measurements": measurements,
        "strategy_summaries": strategy_summaries,
        "profiles": profiles,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _bootstrap_seed_intervals(
    strategy_rows: list[dict[str, Any]],
    *,
    random_seed: int,
    bootstrap_runs: int = 2000,
) -> list[dict[str, Any]]:
    """Bootstrap seed-level medians; raw in-process timings are not independent."""

    grouped: dict[tuple[str, int, str], list[float]] = {}
    for row in strategy_rows:
        key = (str(row["scenario_id"]), int(row["row_count"]), str(row["strategy_id"]))
        grouped.setdefault(key, []).append(float(row["median_governed_latency_ms"]))
    rng = random.Random(random_seed)
    intervals: list[dict[str, Any]] = []
    for (scenario_id, row_count, strategy_id), values in grouped.items():
        bootstrap_medians: list[float] = []
        for _ in range(bootstrap_runs):
            sample = [rng.choice(values) for _ in values]
            bootstrap_medians.append(statistics.median(sample))
        ordered = sorted(bootstrap_medians)
        lower = ordered[round(0.025 * (len(ordered) - 1))]
        upper = ordered[round(0.975 * (len(ordered) - 1))]
        intervals.append(
            {
                "scenario_id": scenario_id,
                "row_count": row_count,
                "strategy_id": strategy_id,
                "seed_count": len(values),
                "median_governed_latency_ms": statistics.median(values),
                "bootstrap_seed_ci95_lower_ms": lower,
                "bootstrap_seed_ci95_upper_ms": upper,
            }
        )
    return intervals


def _finalize(output_dir: Path, config: Phase2CConfig, run_id: str) -> None:
    unit_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((output_dir / "units").glob("*.json"))
    ]
    measurements: list[dict[str, Any]] = []
    strategy_rows: list[dict[str, Any]] = []
    unit_rows: list[dict[str, Any]] = []
    winner_counts: dict[str, int] = {}
    for unit in unit_payloads:
        measurements.extend(unit["measurements"])
        for strategy in unit["strategy_summaries"]:
            strategy_rows.append(
                {
                    "run_id": run_id,
                    "unit_id": unit["unit_id"],
                    "scenario_id": unit["scenario"]["scenario_id"],
                    "row_count": unit["row_count"],
                    "data_seed": unit["data_seed"],
                    **strategy,
                }
            )
        winner = str(unit["observed_winner"])
        winner_counts[winner] = winner_counts.get(winner, 0) + 1
        unit_rows.append(
            {
                "run_id": run_id,
                "unit_id": unit["unit_id"],
                "scenario_id": unit["scenario"]["scenario_id"],
                "row_count": unit["row_count"],
                "data_seed": unit["data_seed"],
                "unique_physical_plan_count": unit["unique_physical_plan_count"],
                "output_row_count": unit["output_row_count"],
                "observed_winner": winner,
                "winner_gap_fraction": unit["winner_gap_fraction"],
            }
        )
    intervals = _bootstrap_seed_intervals(strategy_rows, random_seed=config.order_seed)
    _write_csv(output_dir / "raw_measurements.csv", measurements)
    _write_csv(output_dir / "strategy_summary.csv", strategy_rows)
    _write_csv(output_dir / "unit_summary.csv", unit_rows)
    _write_json_atomic(output_dir / "confidence_intervals.json", intervals)
    _write_json_atomic(
        output_dir / "summary.json",
        {
            "run_id": run_id,
            "status": "complete",
            "unit_count": len(unit_payloads),
            "measurement_count": len(measurements),
            "all_results_equivalent": all(
                bool(item["result_equivalent"]) for item in unit_payloads
            ),
            "units_with_temp_spill": sum(
                any(
                    int(profile["peak_temp_directory_bytes"]) > 0
                    for profile in item["profiles"].values()
                )
                for item in unit_payloads
            ),
            "winner_counts": winner_counts,
            "tie_threshold_fraction": config.tie_threshold_fraction,
            "note": (
                "Confidence intervals bootstrap independent data-seed medians; "
                "they are screening estimates when seed_count is small."
            ),
        },
    )


def run_phase2c(
    config: Phase2CConfig,
    *,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or resume Phase 2C without admitting partial units to summaries."""

    import duckdb

    root = _repo_root()
    commit_hash = _git_commit(root)
    git_dirty = _git_dirty(root)
    if config.require_clean_git and git_dirty:
        raise ValueError("This Phase 2C protocol requires a clean Git worktree")
    results_root = root / config.results_dir
    results_root.mkdir(parents=True, exist_ok=True)
    run_id = resume_run_id or _new_run_id()
    output_dir = results_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    config_payload = asdict(config)
    config_digest = _digest(config_payload)
    state_path = output_dir / "checkpoint.json"
    if resume_run_id:
        if not state_path.exists():
            raise ValueError(f"Cannot resume missing Phase 2C run: {resume_run_id}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("config_digest") != config_digest:
            raise ValueError("Resume config does not match the original Phase 2C run")
        environment = json.loads((output_dir / "environment.json").read_text(encoding="utf-8"))
        if environment.get("commit_hash") != commit_hash:
            raise ValueError("Cannot resume Phase 2C after the Git commit changed")
    else:
        state = {
            "run_id": run_id,
            "config_digest": config_digest,
            "completed_units": [],
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write_json_atomic(output_dir / "config.json", config_payload)
        _write_json_atomic(
            output_dir / "environment.json",
            _environment(commit_hash, git_dirty, config),
        )
        _write_json_atomic(state_path, state)
        _write_json_atomic(results_root / "latest_run.json", {"run_id": run_id})

    logical_plan = build_phase2_experiment_plan(
        source_lineage=config.source_lineage,
        mask_event_id=config.mask_event_id,
    )
    candidates = generate_duckdb_candidates(
        logical_plan,
        materialization_targets=config.materialization_targets,
        filter_orders=config.filter_orders,
        operator_placements=config.operator_placements,
    )
    units = tuple(
        (scenario, row_count, data_seed)
        for scenario in config.scenarios
        for row_count in config.row_counts
        for data_seed in config.seeds
    )
    completed = set(state["completed_units"])
    reporter = _ProgressReporter(
        output_dir=output_dir,
        latest_path=results_root / "latest_progress.json",
        run_id=run_id,
        show_console=show_progress,
    )
    log_path = output_dir / "run.log"
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        for unit_index, (scenario, row_count, data_seed) in enumerate(units):
            unit_id = _unit_id(scenario.scenario_id, row_count, data_seed)
            if unit_id in completed:
                continue
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{datetime.now(UTC).isoformat()} START {unit_id}\n")
            try:
                payload = _run_unit(
                    connection,
                    config=config,
                    scenario=scenario,
                    row_count=row_count,
                    data_seed=data_seed,
                    run_id=run_id,
                    commit_hash=commit_hash,
                    logical_plan=logical_plan,
                    candidates=candidates,
                    unit_index=unit_index,
                    unit_count=len(units),
                    output_dir=output_dir,
                    reporter=reporter,
                )
            except Exception as exc:
                _write_json_atomic(
                    output_dir / "failures" / f"{unit_id}.json",
                    {
                        "unit_id": unit_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
                raise
            _write_json_atomic(output_dir / "units" / f"{unit_id}.json", payload)
            completed.add(unit_id)
            state["completed_units"] = sorted(completed)
            state["updated_at"] = datetime.now(UTC).isoformat()
            _write_json_atomic(state_path, state)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{datetime.now(UTC).isoformat()} COMPLETE {unit_id}\n")
    finally:
        connection.close()

    state["status"] = "complete"
    state["completed_at"] = datetime.now(UTC).isoformat()
    _write_json_atomic(state_path, state)
    _finalize(output_dir, config, run_id)
    reporter.finish(len(units))
    return output_dir


def load_phase2c_config(path: str | Path) -> Phase2CConfig:
    """Load the versioned JSON experiment protocol used by the CLI."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = tuple(Phase2CScenario(**item) for item in payload["scenarios"])
    return Phase2CConfig(
        results_dir=str(payload["results_dir"]),
        scenarios=scenarios,
        row_counts=tuple(int(item) for item in payload["row_counts"]),
        seeds=tuple(int(item) for item in payload["seeds"]),
        warmup_runs=int(payload.get("warmup_runs", 5)),
        measured_runs=int(payload.get("measured_runs", 30)),
        duckdb_threads=int(payload.get("duckdb_threads", 4)),
        duckdb_memory_limit_mb=int(payload.get("duckdb_memory_limit_mb", 4096)),
        order_seed=int(payload.get("order_seed", 20260715)),
        tie_threshold_fraction=float(payload.get("tie_threshold_fraction", 0.03)),
        source_lineage=bool(payload.get("source_lineage", True)),
        require_clean_git=bool(payload.get("require_clean_git", False)),
        materialization_targets=tuple(
            payload.get(
                "materialization_targets",
                supported_phase2_materialization_targets(),
            )
        ),
        filter_orders=tuple(
            tuple(str(operator_id) for operator_id in order)
            for order in payload.get("filter_orders", ())
        ),
        mask_event_id=bool(payload.get("mask_event_id", False)),
        operator_placements=tuple(
            (str(item[0]), str(item[1])) for item in payload.get("operator_placements", ())
        ),
    )
