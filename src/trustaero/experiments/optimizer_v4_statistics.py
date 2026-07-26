"""Extract label-free January statistics for the Optimizer V4 contract.

This is a development preflight, not a performance experiment.  It reads the
same January BTS snapshot used by the frozen V3 transfer audit, but it never
runs or times either physical candidate.  Its only purpose is to prove that
the V4 inputs can be obtained before candidate ranking.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.data import verify_bts_mask_join_full_month_artifacts
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _git_state, _Progress
from trustaero.experiments.real_optimizer_transfer import (
    RealOptimizerTransferConfig,
    _controlled_views,
)
from trustaero.optimizer.mask import MaskPlacement
from trustaero.optimizer.mask_pipeline_v4 import (
    RealPipelineWorkloadStats,
    candidate_work_delta,
    derive_candidate_pipeline_work,
)


def extract_january_pipeline_statistics(
    connection: Any,
    project_root: Path,
    *,
    width: int,
    target_match_rate: float,
) -> tuple[RealPipelineWorkloadStats, dict[str, object]]:
    """Extract one scenario's inputs without observing candidate execution.

    Exact counts are acceptable here because the January scenario is a
    controlled development partition.  The provenance is explicit; a future
    external partition must use the frozen estimator available at that time.
    """

    _, join_input_rows, achieved_rate, selected_count = _controlled_views(
        connection,
        project_root,
        width=width,
        target_match_rate=target_match_rate,
    )
    base = project_root / "data/processed/bts/on_time/2024-01"
    flights = base / "bts_flights_full.parquet"

    # The 25 fixed bytes are the logical widths of FlightDate (8), airport id
    # (8), Distance (8), and Cancelled (1).  Tail_Number is read natively from
    # Parquet; the controlled wide value is derived later and counted
    # separately, so it must not inflate source-scan bytes.
    source = connection.execute(
        "SELECT count(*)::BIGINT, "
        "avg(25 + length(coalesce(CAST(Tail_Number AS VARCHAR), "
        "'UNKNOWN-' || CAST(OriginAirportID AS VARCHAR))))::DOUBLE "
        f"FROM read_parquet({_sql_literal(flights)})"
    ).fetchone()
    dimension = connection.execute(
        "SELECT count(*)::BIGINT, "
        "avg(8 + length(airport_code) + length(city_name) + "
        "length(state_code))::DOUBLE, "
        "avg(length(airport_code) + length(city_name) + "
        "length(state_code))::DOUBLE, "
        "avg(length(airport_code))::DOUBLE "
        "FROM trust_bts_mp_airports"
    ).fetchone()
    output = connection.execute(
        "SELECT count(*)::BIGINT FROM trust_bts_mp_flights AS f "
        "INNER JOIN trust_bts_mp_airports AS a "
        "ON f.OriginAirportID = a.airport_id "
        "WHERE f.FlightDate >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00' "
        "AND f.FlightDate < TIMESTAMPTZ '2024-01-22 00:00:00+00:00' "
        "AND f.Distance >= 750.0 AND f.Cancelled = false "
        "AND f.OriginAirportID IS NOT NULL"
    ).fetchone()
    if source is None or dimension is None or output is None:
        raise GovernedRealDataSmokeError("V4 statistic extraction returned no row")
    if int(dimension[0]) != selected_count or selected_count <= 0:
        raise GovernedRealDataSmokeError("Controlled dimension subset changed")
    join_output_rows = int(output[0])
    expected_output = round(join_input_rows * achieved_rate)
    if join_output_rows != expected_output:
        raise GovernedRealDataSmokeError("Controlled Join estimate is not exact")

    stats = RealPipelineWorkloadStats(
        source_scan_rows=int(source[0]),
        join_input_rows=join_input_rows,
        join_output_rows_estimate=join_output_rows,
        dimension_build_rows=int(dimension[0]),
        sensitive_raw_width_bytes=float(width),
        source_scan_payload_width_bytes=float(source[1]),
        # OriginAirportID and Distance are the fact fields carried into Join.
        join_fact_fixed_width_bytes=16.0,
        dimension_build_payload_width_bytes=float(dimension[1]),
        dimension_output_payload_width_bytes=float(dimension[2]),
        # The final fixed payload is Distance; Tail and dimension strings are
        # charged by their own fields in the candidate work contract.
        output_fixed_width_bytes=8.0,
        sort_key_width_bytes=8.0 + float(dimension[3]),
        statistic_provenance="catalog_exact_controlled",
    )
    return stats, {
        "target_match_rate": target_match_rate,
        "achieved_match_rate": achieved_rate,
        "selected_airport_count": selected_count,
        "candidate_runtime_observed": False,
        "oracle_label_observed": False,
        "external_partition_accessed": False,
    }


def run_optimizer_v4_statistics_preflight(
    config: RealOptimizerTransferConfig,
    *,
    project_root: Path,
    show_progress: bool = False,
) -> Path:
    """Write all January feature records and a label-free preflight summary."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for V4 preflight") from exc

    root = project_root.resolve()
    verify_bts_mask_join_full_month_artifacts(root / "data")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / "results/optimizer_v4_statistics_development" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    progress = _Progress(
        len(config.identifier_widths) * len(config.target_match_rates), show_progress
    )
    # JSON-shaped records mix nested mappings and scalar gate values.
    families: list[dict[str, Any]] = []
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        for width in config.identifier_widths:
            for rate in config.target_match_rates:
                stats, metadata = extract_january_pipeline_statistics(
                    connection, root, width=width, target_match_rate=rate
                )
                early = derive_candidate_pipeline_work(stats, MaskPlacement.EARLY)
                late = derive_candidate_pipeline_work(stats, MaskPlacement.LATE)
                family_id = f"bts-jan-w{width}-target{rate:.2f}"
                families.append(
                    {
                        "family_id": family_id,
                        "statistics": stats.to_dict(),
                        "metadata": metadata,
                        "candidate_work": {
                            MaskPlacement.EARLY.value: early.to_dict(),
                            MaskPlacement.LATE.value: late.to_dict(),
                        },
                        "early_minus_late_feature_delta": list(candidate_work_delta(stats)),
                    }
                )
                progress.advance(f"statistics {family_id}")
    finally:
        connection.close()

    commit, dirty = _git_state(root)
    gate_passed = all(
        not family["metadata"]["candidate_runtime_observed"]
        and not family["metadata"]["oracle_label_observed"]
        and not family["metadata"]["external_partition_accessed"]
        for family in families
    )
    _atomic_json(run_dir / "families.json", families)
    _atomic_json(
        run_dir / "summary.json",
        {
            "schema_version": 1,
            "status": "PASS" if gate_passed else "FAIL",
            "scientific_label": "january_v4_statistics_preflight_not_performance",
            "family_count": len(families),
            "feature_contract_frozen": True,
            "candidate_runtime_observed": False,
            "oracle_label_observed": False,
            "external_partition_accessed": False,
            "model_fitted": False,
            "git_commit": commit,
            "git_dirty": dirty,
            "config": asdict(config),
            "interpretation": (
                "This preflight proves only that candidate-level V4 work inputs "
                "are reproducibly available before ranking on January development "
                "data. It is neither a fitted optimizer nor paper evidence."
            ),
        },
    )
    _atomic_json(
        run_dir.parent / "latest_run.json",
        {"run_id": run_id, "status": "PASS" if gate_passed else "FAIL"},
    )
    return run_dir
