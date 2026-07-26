"""Expanded January paired calibration matrix for Optimizer V4.

The runner creates no model.  It collects authoritative paired wall-clock
labels and pre-execution candidate-work records over non-overlapping January
windows.  February--December remain outside the development partition.
"""

from __future__ import annotations

import csv
import hashlib
import os
import statistics
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_bts_mask_join_full_month_artifacts
from trustaero.execution import (
    CompiledQuery,
    TableBindings,
    compile_approved_physical_plan,
    observe_duckdb_plan,
)
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _git_state, _Progress
from trustaero.experiments.real_optimizer_transfer import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    _candidate_id,
    _materialize,
)
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import (
    ApprovedPhysicalPlan,
    Mask,
    PolicySet,
    ValidatedLogicalPlan,
)
from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import (
    RealPipelineWorkloadStats,
    derive_candidate_pipeline_work,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility.source_freeze import sha256_file
from trustaero.validator.service import validate


@dataclass(frozen=True, slots=True)
class JanuaryDevelopmentWindow:
    """One complete scenario group held together during cross-validation."""

    window_id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not self.window_id or self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("January windows require an ID and timezone-aware bounds")
        if self.start >= self.end:
            raise ValueError("January window start must precede end")
        if self.start.year != 2024 or self.start.month != 1:
            raise ValueError("V4 development windows must begin in January 2024")
        if self.end > datetime.fromisoformat("2024-02-01T00:00:00+00:00"):
            raise ValueError("V4 development windows may not open February data")

    def to_dict(self) -> dict[str, str]:
        return {
            "window_id": self.window_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class OptimizerV4CalibrationConfig:
    """Immutable protocol for expanded January paired measurements."""

    protocol_name: str
    results_dir: str
    windows: tuple[JanuaryDevelopmentWindow, ...]
    identifier_widths: tuple[int, ...]
    target_match_rates: tuple[float, ...]
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    tie_threshold_fraction: float
    require_clean_git: bool
    profile_analysis_path: str
    profile_analysis_sha256: str
    scientific_boundary: str

    def __post_init__(self) -> None:
        if not self.protocol_name or not self.results_dir or not self.windows:
            raise ValueError("V4 calibration protocol identity and windows are required")
        ids = [item.window_id for item in self.windows]
        if len(ids) != len(set(ids)):
            raise ValueError("January window IDs must be unique")
        ordered = sorted(self.windows, key=lambda item: item.start)
        if any(left.end > right.start for left, right in zip(ordered, ordered[1:], strict=False)):
            raise ValueError("January development windows must not overlap")
        if not self.identifier_widths or any(width < 64 for width in self.identifier_widths):
            raise ValueError("Calibration widths must be at least the digest width")
        if not self.target_match_rates or any(
            not 0.0 < rate <= 1.0 for rate in self.target_match_rates
        ):
            raise ValueError("Calibration match rates must be in (0, 1]")
        if self.warmup_blocks < 0 or self.warmup_blocks % 2:
            raise ValueError("Warmups must cover complete two-candidate permutations")
        if self.measured_blocks < 2 or self.measured_blocks % 2:
            raise ValueError("Measurements must cover complete two-candidate permutations")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 512:
            raise ValueError("DuckDB calibration controls are invalid")
        if not 0.0 <= self.tie_threshold_fraction < 1.0:
            raise ValueError("Calibration tie threshold must be a fraction")
        if len(self.profile_analysis_sha256) != 64:
            raise ValueError("Profile-analysis binding must be SHA-256")

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["windows"] = [item.to_dict() for item in self.windows]
        return payload


def load_optimizer_v4_calibration_config(
    path: Path | str,
) -> OptimizerV4CalibrationConfig:
    """Load the predeclared January calibration protocol."""

    payload = cast(dict[str, Any], _load_json(Path(path)))
    windows = tuple(
        JanuaryDevelopmentWindow(
            window_id=str(item["window_id"]),
            start=datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00")),
            end=datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00")),
        )
        for item in cast(list[dict[str, Any]], payload["windows"])
    )
    return OptimizerV4CalibrationConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        windows=windows,
        identifier_widths=tuple(int(item) for item in payload["identifier_widths"]),
        target_match_rates=tuple(float(item) for item in payload["target_match_rates"]),
        warmup_blocks=int(payload["warmup_blocks"]),
        measured_blocks=int(payload["measured_blocks"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
        profile_analysis_path=str(payload["profile_analysis_path"]),
        profile_analysis_sha256=str(payload["profile_analysis_sha256"]),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _build_window_candidates(
    root: Path,
    window: JanuaryDevelopmentWindow,
) -> tuple[
    ValidatedLogicalPlan,
    InMemoryCatalog,
    tuple[ApprovedPhysicalPlan, ApprovedPhysicalPlan],
]:
    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_mask_join_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_mask_join_policy.json"))
    raw = cast(
        dict[str, Any],
        _load_json(examples / "plans/bts_mask_optimizer_transfer.json"),
    )
    raw["plan_id"] = f"real-bts-mask-v4-{window.window_id}"
    temporal = [
        item
        for item in cast(list[dict[str, Any]], raw["operators"])
        if item.get("operator_type") == "TemporalFilter"
    ]
    if len(temporal) != 1:
        raise GovernedRealDataSmokeError("V4 base plan temporal shape changed")
    temporal[0]["start"] = window.start.isoformat()
    temporal[0]["end"] = window.end.isoformat()
    response = validate(raw, policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("V4 window plan failed validation")
    logical = response.validated_plan
    masks = [item for item in logical.operators if isinstance(item, Mask)]
    if len(masks) != 1:
        raise GovernedRealDataSmokeError("V4 window Mask contract changed")
    generated = generate_duckdb_candidates(
        logical,
        materialized_operator_placements=((masks[0].operator_id, "bts-mp-project"),),
    )
    if len(generated) != 2:
        raise GovernedRealDataSmokeError("V4 window candidate pair changed")
    return logical, catalog, (generated[0], generated[1])


def _window_predicate(window: JanuaryDevelopmentWindow, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    start = _sql_literal(window.start.isoformat())
    end = _sql_literal(window.end.isoformat())
    return (
        f"{prefix}FlightDate >= CAST({start} AS TIMESTAMPTZ) AND "
        f"{prefix}FlightDate < CAST({end} AS TIMESTAMPTZ) AND "
        f"{prefix}Distance >= 750.0 AND {prefix}Cancelled = false AND "
        f"{prefix}OriginAirportID IS NOT NULL"
    )


def _controlled_window_views(
    connection: Any,
    root: Path,
    window: JanuaryDevelopmentWindow,
    *,
    width: int,
    target_match_rate: float,
) -> tuple[TableBindings, int, int, float, int]:
    """Create deterministic real-row views for one frozen time window."""

    base = root / "data/processed/bts/on_time/2024-01"
    flights = base / "bts_flights_full.parquet"
    airports = base / "bts_airports.parquet"
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mp_flights AS SELECT "
        "CAST(FlightDate AS TIMESTAMPTZ) AS FlightDate, "
        "CAST(OriginAirportID AS BIGINT) AS OriginAirportID, "
        "rpad(substr(coalesce(CAST(Tail_Number AS VARCHAR), "
        f"'UNKNOWN-' || CAST(OriginAirportID AS VARCHAR)), 1, {width}), "
        f"{width}, 'x') AS Tail_Number, "
        "CAST(Distance AS DOUBLE) AS Distance, CAST(Cancelled AS BOOLEAN) AS Cancelled "
        f"FROM read_parquet({_sql_literal(flights)})"
    )
    weighted = connection.execute(
        "SELECT OriginAirportID, count(*)::BIGINT AS rows "
        "FROM trust_bts_mp_flights WHERE " + _window_predicate(window) + " GROUP BY OriginAirportID"
    ).fetchall()
    available = {
        int(row[0])
        for row in connection.execute(
            f"SELECT airport_id FROM read_parquet({_sql_literal(airports)})"
        ).fetchall()
    }
    all_counts = [(int(key), int(rows)) for key, rows in weighted]
    counts = [(key, rows) for key, rows in all_counts if key in available]
    counts.sort(
        key=lambda item: hashlib.sha256(
            f"trustaero-v4:{window.window_id}:{item[0]}".encode()
        ).hexdigest()
    )
    join_input_rows = sum(rows for _, rows in all_counts)
    target_rows = target_match_rate * join_input_rows
    selected: list[int] = []
    matched_rows = 0
    for airport_id, rows in counts:
        if matched_rows >= target_rows:
            break
        selected.append(airport_id)
        matched_rows += rows
    if join_input_rows <= 0 or not selected:
        raise GovernedRealDataSmokeError("V4 window produced an empty controlled Join")
    connection.execute("DROP TABLE IF EXISTS transfer_airport_ids")
    connection.execute("CREATE TEMP TABLE transfer_airport_ids(airport_id BIGINT PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO transfer_airport_ids VALUES (?)", [(item,) for item in selected]
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mp_airports AS SELECT "
        "CAST(source.airport_id AS BIGINT) AS airport_id, "
        "CAST(airport_code AS VARCHAR) AS airport_code, "
        "CAST(city_name AS VARCHAR) AS city_name, "
        "CAST(state_code AS VARCHAR) AS state_code "
        f"FROM read_parquet({_sql_literal(airports)}) AS source "
        "INNER JOIN transfer_airport_ids AS selected USING (airport_id)"
    )
    return (
        TableBindings(
            dataset_tables={
                "bts_on_time_2024_01_mask_join": "trust_bts_mp_flights",
                "bts_airports_2024_01_mask_join": "trust_bts_mp_airports",
            }
        ),
        join_input_rows,
        matched_rows,
        matched_rows / join_input_rows,
        len(selected),
    )


def _window_statistics(
    connection: Any,
    root: Path,
    *,
    join_input_rows: int,
    matched_rows: int,
    selected_count: int,
    width: int,
) -> RealPipelineWorkloadStats:
    flights = root / "data/processed/bts/on_time/2024-01/bts_flights_full.parquet"
    source = connection.execute(
        "SELECT count(*)::BIGINT, "
        "avg(25 + length(coalesce(CAST(Tail_Number AS VARCHAR), "
        "'UNKNOWN-' || CAST(OriginAirportID AS VARCHAR))))::DOUBLE "
        f"FROM read_parquet({_sql_literal(flights)})"
    ).fetchone()
    dimension = connection.execute(
        "SELECT count(*)::BIGINT, "
        "avg(8 + length(airport_code) + length(city_name) + length(state_code))::DOUBLE, "
        "avg(length(airport_code) + length(city_name) + length(state_code))::DOUBLE, "
        "avg(length(airport_code))::DOUBLE FROM trust_bts_mp_airports"
    ).fetchone()
    if source is None or dimension is None or int(dimension[0]) != selected_count:
        raise GovernedRealDataSmokeError("V4 window statistics changed unexpectedly")
    return RealPipelineWorkloadStats(
        source_scan_rows=int(source[0]),
        join_input_rows=join_input_rows,
        join_output_rows_estimate=matched_rows,
        dimension_build_rows=selected_count,
        sensitive_raw_width_bytes=float(width),
        source_scan_payload_width_bytes=float(source[1]),
        join_fact_fixed_width_bytes=16.0,
        dimension_build_payload_width_bytes=float(dimension[1]),
        dimension_output_payload_width_bytes=float(dimension[2]),
        output_fixed_width_bytes=8.0,
        sort_key_width_bytes=8.0 + float(dimension[3]),
        statistic_provenance="catalog_exact_controlled",
    )


def _run_family(
    connection: Any,
    root: Path,
    config: OptimizerV4CalibrationConfig,
    window: JanuaryDevelopmentWindow,
    *,
    width: int,
    target_match_rate: float,
    family_index: int,
    progress: _Progress,
) -> dict[str, object]:
    logical, catalog, candidates = _build_window_candidates(root, window)
    bindings, rows, matched, achieved, selected_count = _controlled_window_views(
        connection,
        root,
        window,
        width=width,
        target_match_rate=target_match_rate,
    )
    stats = _window_statistics(
        connection,
        root,
        join_input_rows=rows,
        matched_rows=matched,
        selected_count=selected_count,
        width=width,
    )
    compiled: dict[str, CompiledQuery] = {}
    checksums: dict[str, tuple[Any, ...]] = {}
    plans: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        query = compile_approved_physical_plan(logical, candidate, catalog, bindings)
        observation = observe_duckdb_plan(connection, query.sql, query.parameters, analyze=True)
        compiled[candidate_id] = query
        checksums[candidate_id] = _materialize(connection, query)
        plans[candidate_id] = {
            "fingerprint": observation.fingerprint,
            "operator_names": list(observation.operator_names),
            "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
            "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
        }
        progress.advance(f"preflight {window.window_id} w{width} {candidate_id}")
    candidate_ids = (EARLY_CANDIDATE, LATE_CANDIDATE)
    warmups = complete_permutation_orders(
        candidate_ids,
        config.warmup_blocks,
        seed=config.order_seed + family_index * 2,
    )
    measured = complete_permutation_orders(
        candidate_ids,
        config.measured_blocks,
        seed=config.order_seed + family_index * 2 + 1,
    )
    timings: list[dict[str, Any]] = []
    for measured_run, orders in ((False, warmups), (True, measured)):
        for block_index, order in enumerate(orders):
            for position, candidate_id in enumerate(order):
                started = time.perf_counter_ns()
                checksum = _materialize(connection, compiled[candidate_id])
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                if checksum != checksums[candidate_id]:
                    raise GovernedRealDataSmokeError("V4 calibration result changed")
                if measured_run:
                    timings.append(
                        {
                            "block_index": block_index,
                            "permutation_id": " -> ".join(order),
                            "order_position": position,
                            "candidate_id": candidate_id,
                            "latency_ms": latency_ms,
                        }
                    )
                progress.advance(
                    f"{'measure' if measured_run else 'warmup'} "
                    f"{window.window_id} w{width} {candidate_id}"
                )
    medians = {
        candidate_id: statistics.median(
            float(item["latency_ms"]) for item in timings if item["candidate_id"] == candidate_id
        )
        for candidate_id in candidate_ids
    }
    strict_stats = replace(stats, max_raw_exposure_rows=0)
    result_equal = len(set(checksums.values())) == 1
    plans_distinct = len({str(item["fingerprint"]) for item in plans.values()}) == 2
    no_spill = all(int(item["peak_temp_directory_bytes"]) == 0 for item in plans.values())
    governance_gate = strict_stats.placement_is_legal(
        MaskPlacement.EARLY
    ) and not strict_stats.placement_is_legal(MaskPlacement.LATE)
    return {
        "family_id": f"{window.window_id}-w{width}-target{target_match_rate:.2f}",
        "scenario_group": window.window_id,
        "window": window.to_dict(),
        "status": "PASS"
        if result_equal and plans_distinct and no_spill and governance_gate
        else "FAIL",
        "identifier_width_bytes": width,
        "target_match_rate": target_match_rate,
        "achieved_join_match_rate": achieved,
        "join_input_rows": rows,
        "join_output_rows": matched,
        "selected_airport_count": selected_count,
        "statistics": stats.to_dict(),
        "candidate_work": {
            MaskPlacement.EARLY.value: derive_candidate_pipeline_work(
                stats, MaskPlacement.EARLY
            ).to_dict(),
            MaskPlacement.LATE.value: derive_candidate_pipeline_work(
                stats, MaskPlacement.LATE
            ).to_dict(),
        },
        "strict_policy_allows_only_early": governance_gate,
        "result_equivalent": result_equal,
        "physical_plans_distinct": plans_distinct,
        "no_spill": no_spill,
        "candidate_median_ms": medians,
        "plans": plans,
        "semantic_checksum": [str(item) for item in next(iter(checksums.values()))],
        "timings": timings,
    }


def _write_measurements(path: Path, families: list[dict[str, Any]]) -> None:
    rows = [
        {
            "family_id": family["family_id"],
            "scenario_group": family["scenario_group"],
            "identifier_width_bytes": family["identifier_width_bytes"],
            "target_match_rate": family["target_match_rate"],
            "achieved_join_match_rate": family["achieved_join_match_rate"],
            "join_input_rows": family["join_input_rows"],
            **timing,
        }
        for family in families
        for timing in cast(list[dict[str, Any]], family["timings"])
    ]
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _summarize(
    config: OptimizerV4CalibrationConfig,
    families: list[dict[str, Any]],
) -> dict[str, object]:
    passed = all(item["status"] == "PASS" for item in families)
    return {
        "schema_version": 1,
        "status": "PASS_STRUCTURAL_GATE" if passed else "FAIL_STRUCTURAL_GATE",
        "scientific_label": "expanded_january_v4_development_labels_not_holdout",
        "family_count": len(families),
        "scenario_group_count": len({item["scenario_group"] for item in families}),
        "measurement_count": sum(len(item["timings"]) for item in families),
        "all_semantic_and_physical_gates_pass": passed,
        "model_fitted": False,
        "external_partition_accessed": False,
        "grouped_cross_validation_required": True,
        "tie_threshold_fraction": config.tie_threshold_fraction,
        "scientific_boundary": config.scientific_boundary,
    }


def run_optimizer_v4_calibration(
    config: OptimizerV4CalibrationConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or resume expanded January families with atomic checkpoints."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for V4 calibration") from exc
    root = project_root.resolve()
    verify_bts_mask_join_full_month_artifacts(root / "data")
    if sha256_file(root / config.profile_analysis_path) != config.profile_analysis_sha256:
        raise GovernedRealDataSmokeError("Frozen V4 profile analysis binding changed")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("V4 calibration requires a clean commit")
    results_root = root / config.results_dir
    if resume_run_id is not None:
        if resume_run_id == "latest":
            resume_run_id = str(
                cast(dict[str, Any], _load_json(results_root / "latest_run.json"))["run_id"]
            )
        run_dir = results_root / resume_run_id
        environment = cast(dict[str, Any], _load_json(run_dir / "environment.json"))
        if environment["commit_hash"] != commit:
            raise GovernedRealDataSmokeError("V4 calibration resume commit changed")
        if environment["config_sha256"] != sha256_file(config_path):
            raise GovernedRealDataSmokeError("V4 calibration resume config changed")
    else:
        run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        run_dir = results_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(run_dir / "config.json", config.to_dict())
        _atomic_json(
            run_dir / "environment.json",
            {
                "commit_hash": commit,
                "git_dirty": dirty,
                "config_sha256": sha256_file(config_path),
                "duckdb_threads": config.duckdb_threads,
                "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
                "cache_protocol": "hot_same_connection_within_family",
                "gpu_acceleration": False,
            },
        )
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    family_dir = run_dir / "families"
    family_dir.mkdir(parents=True, exist_ok=True)
    steps = 2 + 2 * (config.warmup_blocks + config.measured_blocks)
    total = (
        len(config.windows) * len(config.identifier_widths) * len(config.target_match_rates) * steps
    )
    progress = _Progress(total, show_progress)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb-optimizer-v4-calibration"
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(temp_dir)}")
        family_index = 0
        for window in config.windows:
            for width in config.identifier_widths:
                for rate in config.target_match_rates:
                    family_id = f"{window.window_id}-w{width}-target{rate:.2f}"
                    target = family_dir / f"{family_id}.json"
                    if target.is_file():
                        for _ in range(steps):
                            progress.advance(f"resume skip {family_id}")
                        family_index += 1
                        continue
                    family = _run_family(
                        connection,
                        root,
                        config,
                        window,
                        width=width,
                        target_match_rate=rate,
                        family_index=family_index,
                        progress=progress,
                    )
                    _atomic_json(target, family)
                    _atomic_json(
                        run_dir / "checkpoint.json",
                        {"last_completed_family": family_id},
                    )
                    family_index += 1
    finally:
        connection.close()
    families = [
        cast(dict[str, Any], _load_json(path)) for path in sorted(family_dir.glob("*.json"))
    ]
    expected = len(config.windows) * len(config.identifier_widths) * len(config.target_match_rates)
    if len(families) != expected:
        raise GovernedRealDataSmokeError("V4 calibration is incomplete; resume it")
    _write_measurements(run_dir / "measurements.csv", families)
    _atomic_json(run_dir / "summary.json", _summarize(config, families))
    return run_dir
