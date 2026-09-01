"""Out-of-time performance admission for the BTS natural multi-Join family."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import os
import platform
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data.download import sha256_file
from trustaero.execution import (
    CompiledQuery,
    TableBindings,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.bts_multijoin import BTS_MULTIJOIN_TARGETS
from trustaero.experiments.execution_aware_oracle_stability import (
    classify_ratio_interval,
    confidence_undominated_set,
)
from trustaero.experiments.execution_flow_inference import hierarchical_paired_log_ratio_ci
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_candidates import verify_candidate_execution_certificate
from trustaero.experiments.real_data_governed import _atomic_json, _load_json, _sql_literal
from trustaero.experiments.real_data_pilot import _git_state, _semantic_digest
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class TemporalTiming:
    month: str
    block_index: int
    permutation_id: str
    order_position: int
    candidate_id: str
    latency_ms: float
    process_cpu_ms: float
    output_row_count: int
    semantic_result_digest: str


def _object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return cast(JsonObject, value)


def _stable_seed(base: int, label: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{base}:{label}".encode()).digest()[:8], "big")


def _next_month(month: str) -> str:
    year, number = (int(value) for value in month.split("-"))
    return f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"


def _month_models(root: Path, month: str) -> tuple[JsonObject, JsonObject, JsonObject]:
    base = root / "data/processed/bts/on_time" / month
    versions = {
        "flight": f"v{month}-{sha256_file(base / 'bts_flights_full.parquet')[:12]}",
        "airport": f"v{month}-{sha256_file(base / 'bts_airports.parquet')[:12]}",
        "carrier": f"v{month}-{sha256_file(base / 'bts_carriers.parquet')[:12]}",
    }
    datasets = {
        "flight": f"bts_on_time_{month.replace('-', '_')}_multijoin",
        "airport": f"bts_airports_{month.replace('-', '_')}",
        "carrier": f"bts_carriers_{month.replace('-', '_')}",
    }
    examples = root / "examples/real_data"
    import copy

    catalog = copy.deepcopy(_load_json(examples / "bts_multijoin_catalog.json"))
    policy = copy.deepcopy(_load_json(examples / "bts_multijoin_policy.json"))
    plan = copy.deepcopy(_load_json(examples / "plans/bts_natural_multijoin.json"))
    for item, key in zip(catalog["datasets"], ("flight", "airport", "carrier"), strict=True):
        item["dataset_id"] = datasets[key]
        item["versions"] = [versions[key]]
        item["default_version"] = versions[key]
    scan_map = {
        "bts-mj-flight-scan": (datasets["flight"], versions["flight"]),
        "bts-mj-airport-scan": (datasets["airport"], versions["airport"]),
        "bts-mj-carrier-scan": (datasets["carrier"], versions["carrier"]),
    }
    for operator in plan["operators"]:
        if operator["operator_id"] in scan_map:
            operator["dataset"], operator["snapshot"] = scan_map[operator["operator_id"]]
        if operator["operator_id"] == "bts-mj-time":
            operator["start"] = f"{month}-01T00:00:00+00:00"
            operator["end"] = f"{_next_month(month)}-01T00:00:00+00:00"
    plan["plan_id"] = f"real-bts-natural-multijoin-temporal-{month}"
    policy["policy_snapshot"] = f"policy-bts-multijoin-{month}-temporal-v1"
    for rule in policy["rules"]:
        rule["resources"] = list(datasets.values())
    return catalog, policy, plan


def _bindings(connection: Any, root: Path, month: str) -> TableBindings:
    base = root / "data/processed/bts/on_time" / month
    datasets = {
        "flight": f"bts_on_time_{month.replace('-', '_')}_multijoin",
        "airport": f"bts_airports_{month.replace('-', '_')}",
        "carrier": f"bts_carriers_{month.replace('-', '_')}",
    }
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mj_flights AS SELECT "
        "CAST(FlightDate AS TIMESTAMPTZ) AS FlightDate, "
        "CAST(OriginAirportID AS BIGINT) AS OriginAirportID, "
        "CAST(DOT_ID_Reporting_Airline AS BIGINT) AS DOT_ID_Reporting_Airline, "
        "CAST(Distance AS DOUBLE) AS Distance, CAST(Cancelled AS BOOLEAN) AS Cancelled "
        f"FROM read_parquet({_sql_literal(base / 'bts_flights_full.parquet')})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mj_airports AS SELECT "
        "CAST(airport_id AS BIGINT) AS airport_id, CAST(airport_code AS VARCHAR) AS airport_code, "
        "CAST(city_name AS VARCHAR) AS city_name, CAST(state_code AS VARCHAR) AS state_code "
        f"FROM read_parquet({_sql_literal(base / 'bts_airports.parquet')})"
    )
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW trust_bts_mj_carriers AS SELECT "
        "CAST(carrier_id AS BIGINT) AS carrier_id, CAST(carrier_code AS VARCHAR) AS carrier_code "
        f"FROM read_parquet({_sql_literal(base / 'bts_carriers.parquet')})"
    )
    return TableBindings(
        dataset_tables={
            datasets["flight"]: "trust_bts_mj_flights",
            datasets["airport"]: "trust_bts_mj_airports",
            datasets["carrier"]: "trust_bts_mj_carriers",
        }
    )


def _verify_manifest(root: Path, month: str) -> JsonObject:
    manifest = root / f"data/manifests/processed/bts-{month}.json"
    payload = _object(manifest)
    if payload.get("status") != "PASS":
        raise ValueError(f"Prepared month did not pass: {month}")
    for item in payload["outputs"]:
        path = root / "data" / item["relative_path"]
        if path.stat().st_size != int(item["byte_size"]) or sha256_file(path) != item["sha256"]:
            raise ValueError(f"Prepared artifact changed: {path}")
    return payload


def _stage_statistics(connection: Any) -> JsonObject:
    row = connection.execute(
        """
        WITH governed AS (
          SELECT * FROM trust_bts_mj_flights WHERE Distance >= 750.0 AND Cancelled = false
        ), origin_joined AS (
          SELECT g.* FROM governed g JOIN trust_bts_mj_airports a
            ON g.OriginAirportID = a.airport_id
        ), carrier_joined AS (
          SELECT o.* FROM origin_joined o JOIN trust_bts_mj_carriers c
            ON o.DOT_ID_Reporting_Airline = c.carrier_id
        )
        SELECT (SELECT COUNT(*) FROM trust_bts_mj_flights),
               (SELECT COUNT(*) FROM governed),
               (SELECT COUNT(*) FROM origin_joined),
               (SELECT COUNT(*) FROM carrier_joined)
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("BTS stage statistics are missing")
    input_rows, governed, origin, carrier = map(int, row)
    return {
        "input_rows": input_rows,
        "governed_rows": governed,
        "governed_selectivity": governed / input_rows,
        "origin_join_rows": origin,
        "origin_join_match_rate": origin / governed,
        "carrier_join_rows": carrier,
        "carrier_join_match_rate": carrier / origin,
    }


def _environment(root: Path, protocol: JsonObject) -> JsonObject:
    commit, dirty = _git_state(root)
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
        "commit_hash": commit,
        "git_dirty": dirty,
        "packages": packages,
        "duckdb_threads": protocol["timing"]["duckdb_threads"],
        "duckdb_memory_limit_mb": protocol["timing"]["duckdb_memory_limit_mb"],
        "cache_protocol": "hot_same_duckdb_connection_with_month_isolation",
    }


def run_temporal_holdout(protocol_path: Path, *, project_root: Path) -> Path:
    root = project_root.resolve()
    protocol = _object(protocol_path)
    if protocol.get("status") != "FROZEN_BEFORE_DATA_ACQUISITION":
        raise ValueError("Temporal protocol is not frozen")
    for item in protocol["immutable_implementation_files"]:
        path = root / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise ValueError(f"Frozen implementation changed: {path}")
    months = tuple(str(value) for value in protocol["months"])
    for month in months:
        _verify_manifest(root, month)

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("DuckDB is required") from exc

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = root / str(protocol["results_dir"]) / run_id
    out.mkdir(parents=True, exist_ok=False)
    _atomic_json(out / "protocol_snapshot.json", protocol)
    _atomic_json(out / "environment.json", _environment(root, protocol))
    rows: list[TemporalTiming] = []
    month_preflights: list[JsonObject] = []
    timing = protocol["timing"]

    for month_index, month in enumerate(months):
        print(f"[temporal {month_index + 1}/{len(months)}] {month}: preflight", flush=True)
        catalog_payload, policy_payload, plan_payload = _month_models(root, month)
        catalog = InMemoryCatalog(CatalogDocument.model_validate(catalog_payload))
        policy = PolicySet.model_validate(policy_payload)
        response = validate(plan_payload, policy, catalog)
        if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
            raise RuntimeError(f"Monthly plan failed validation: {month}")
        logical = response.validated_plan
        candidates = generate_duckdb_candidates(
            logical,
            materialization_targets=BTS_MULTIJOIN_TARGETS,
        )
        candidate_ids = tuple(item.strategy.strategy_id for item in candidates)
        if set(candidate_ids) != set(protocol["candidate_ids"]):
            raise RuntimeError(f"Candidate interface changed: {month}")

        connection = duckdb.connect()
        compiled: dict[str, CompiledQuery] = {}
        fingerprints: set[str] = set()
        expected_digest: str | None = None
        preflight_candidates: list[JsonObject] = []
        try:
            connection.execute(f"SET threads = {int(timing['duckdb_threads'])}")
            connection.execute(f"SET memory_limit = '{int(timing['duckdb_memory_limit_mb'])}MB'")
            temp = root / "data/tmp/duckdb-bts-2025-temporal" / month
            temp.mkdir(parents=True, exist_ok=True)
            connection.execute(f"SET temp_directory = {_sql_literal(temp)}")
            bindings = _bindings(connection, root, month)
            stage = _stage_statistics(connection)
            for candidate in candidates:
                candidate_id = candidate.strategy.strategy_id
                query = compile_approved_physical_plan(logical, candidate, catalog, bindings)
                execution = execute_with_connection(query, connection)
                digest = _semantic_digest(execution.columns, execution.rows)
                if expected_digest is None:
                    expected_digest = digest
                elif digest != expected_digest:
                    raise RuntimeError(f"Candidate outputs differ: {month}")
                certificate = verify_candidate_execution_certificate(
                    logical,
                    candidate,
                    execution,
                    execution_id=f"bts-2025-temporal-{month}-{candidate_id}",
                )
                observation = observe_duckdb_plan(
                    connection,
                    query.sql,
                    query.parameters,
                    analyze=True,
                )
                if observation.fingerprint in fingerprints:
                    raise RuntimeError(f"Physical candidates collapsed: {month}")
                fingerprints.add(observation.fingerprint)
                compiled[candidate_id] = query
                preflight_candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "physical_plan_id": candidate.physical_plan_id,
                        "duckdb_plan_fingerprint": observation.fingerprint,
                        "duckdb_operator_names": list(observation.operator_names),
                        "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                        "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                        "certificate_status": certificate,
                        "result_digest": digest,
                        "output_row_count": execution.row_count,
                    }
                )

            warmups = complete_permutation_orders(
                tuple(compiled),
                int(timing["warmup_blocks"]),
                seed=int(timing["order_seed"]) + month_index * 100,
            )
            measured = complete_permutation_orders(
                tuple(compiled),
                int(timing["measured_blocks"]),
                seed=int(timing["order_seed"]) + month_index * 100 + 1,
            )
            for is_measured, orders in ((False, warmups), (True, measured)):
                for block_index, order in enumerate(orders):
                    permutation_id = " -> ".join(order)
                    for position, candidate_id in enumerate(order):
                        cpu_start = time.process_time_ns()
                        start = time.perf_counter_ns()
                        execution = execute_with_connection(compiled[candidate_id], connection)
                        elapsed = (time.perf_counter_ns() - start) / 1_000_000
                        cpu = (time.process_time_ns() - cpu_start) / 1_000_000
                        digest = _semantic_digest(execution.columns, execution.rows)
                        if digest != expected_digest:
                            raise RuntimeError(f"Timed result changed: {month}")
                        if is_measured:
                            rows.append(
                                TemporalTiming(
                                    month,
                                    block_index,
                                    permutation_id,
                                    position,
                                    candidate_id,
                                    elapsed,
                                    cpu,
                                    execution.row_count,
                                    digest,
                                )
                            )
                    if (block_index + 1) % 12 == 0:
                        print(
                            f"[temporal {month}] {'measure' if is_measured else 'warmup'} "
                            f"block {block_index + 1}/{len(orders)}",
                            flush=True,
                        )
            month_preflights.append(
                {
                    "month": month,
                    "stage_statistics": stage,
                    "candidate_count": len(candidates),
                    "distinct_plan_count": len(fingerprints),
                    "candidates": preflight_candidates,
                }
            )
        finally:
            connection.close()

        with (out / "measurements.csv.tmp").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(TemporalTiming.__annotations__))
            writer.writeheader()
            writer.writerows(asdict(row) for row in rows)
        os.replace(out / "measurements.csv.tmp", out / "measurements.csv")
        _atomic_json(
            out / "progress.json",
            {"completed_months": month_index + 1, "last_month": month},
        )

    _atomic_json(
        out / "raw_summary.json",
        {
            "schema_version": 1,
            "status": "MEASUREMENT_COMPLETE",
            "month_preflights": month_preflights,
            "measurement_count": len(rows),
        },
    )
    return out


def analyze_temporal_holdout(run_dir: Path) -> JsonObject:
    protocol = _object(run_dir / "protocol_snapshot.json")
    raw = _object(run_dir / "raw_summary.json")
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        measurements = list(csv.DictReader(handle))
    candidate_ids = tuple(str(value) for value in protocol["candidate_ids"])
    practical = float(protocol["inference"]["practical_tie_fraction"])
    confidence = float(protocol["inference"]["confidence_level"])
    draws = int(protocol["inference"]["bootstrap_draws"])
    base_seed = int(protocol["inference"]["bootstrap_seed"])
    by_month: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in measurements:
        by_month[row["month"]].append(row)

    monthly: list[JsonObject] = []
    winners: list[str] = []
    for month, month_rows in sorted(by_month.items()):
        blocks: dict[int, dict[str, float]] = defaultdict(dict)
        for row in month_rows:
            blocks[int(row["block_index"])][row["candidate_id"]] = float(row["latency_ms"])
        if any(set(values) != set(candidate_ids) for values in blocks.values()):
            raise ValueError(f"Incomplete paired block: {month}")
        pairwise: list[JsonObject] = []
        for left, right in itertools.combinations(candidate_ids, 2):
            logs = {
                0: [math.log(values[left] / values[right]) for _, values in sorted(blocks.items())]
            }
            point, lower, upper = hierarchical_paired_log_ratio_ci(
                logs,
                confidence_level=confidence,
                repetitions=draws,
                seed=_stable_seed(base_seed, f"{month}:{left}:{right}"),
            )
            pairwise.append(
                {
                    "left_candidate_id": left,
                    "right_candidate_id": right,
                    "left_over_right_ratio": point,
                    "confidence_interval": [lower, upper],
                    "conclusion": classify_ratio_interval(lower, upper, practical),
                }
            )
        confidence_set = confidence_undominated_set(candidate_ids, pairwise)
        winner = confidence_set[0] if len(confidence_set) == 1 else None
        if winner is not None:
            winners.append(winner)
        medians = {
            candidate_id: statistics.median(
                float(row["latency_ms"])
                for row in month_rows
                if row["candidate_id"] == candidate_id
            )
            for candidate_id in candidate_ids
        }
        monthly.append(
            {
                "month": month,
                "candidate_median_ms": medians,
                "confidence_undominated_candidate_ids": list(confidence_set),
                "singleton_winner": winner,
                "pairwise": pairwise,
            }
        )

    expected = (
        len(protocol["months"]) * int(protocol["timing"]["measured_blocks"]) * len(candidate_ids)
    )
    preflights = raw["month_preflights"]
    gates = {
        "all_months_complete": len(monthly) == len(protocol["months"]),
        "all_measurements_complete": len(measurements) == expected,
        "all_candidates_distinct": all(item["distinct_plan_count"] == 4 for item in preflights),
        "all_certificates_partial": all(
            candidate["certificate_status"] == "PARTIAL"
            for item in preflights
            for candidate in item["candidates"]
        ),
        "all_results_equivalent": all(
            len({candidate["result_digest"] for candidate in item["candidates"]}) == 1
            for item in preflights
        ),
    }
    winner_counts = Counter(winners)
    if len(winner_counts) >= 2:
        conclusion = "CROSS_MONTH_SINGLETON_REVERSAL_OBSERVED"
    elif winner_counts:
        conclusion = "SINGLE_SINGLETON_WINNER_OBSERVED"
    else:
        conclusion = "NO_SINGLETON_WINNER_OBSERVED"
    status = "PASS_BTS_2025_TEMPORAL_HOLDOUT_COMPLETED" if all(gates.values()) else "FAIL"
    payload: JsonObject = {
        "schema_version": 1,
        "status": status,
        "scientific_conclusion": conclusion,
        "paper_performance_evidence": status.startswith("PASS"),
        "heldout_optimizer_selection_evidence": False,
        "year": 2025,
        "month_count": len(monthly),
        "candidate_count": len(candidate_ids),
        "measurement_count": len(measurements),
        "singleton_winner_counts": dict(sorted(winner_counts.items())),
        "singleton_month_count": len(winners),
        "gates": gates,
        "monthly_results": monthly,
        "claim_boundary": protocol["claim_boundary"],
    }
    _atomic_json(run_dir / "summary.json", payload)
    return payload
