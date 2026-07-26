"""Formal paired timing for the full-month BTS natural multi-Join.

Four legal routes require 24 execution-order permutations.  The frozen
development protocol repeats every permutation twice, giving each candidate 48
paired observations without pretending January is an optimizer holdout.
"""

from __future__ import annotations

import csv
import json
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
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.data import verify_bts_multijoin_full_month_artifacts
from trustaero.data.download import sha256_file
from trustaero.execution import (
    CompiledQuery,
    compile_approved_physical_plan,
    execute_with_connection,
    observe_duckdb_plan,
)
from trustaero.experiments.bts_multijoin import (
    BTS_MULTIJOIN_TARGETS,
    _create_bts_multijoin_views,
)
from trustaero.experiments.real_data_candidate_pilot import complete_permutation_orders
from trustaero.experiments.real_data_candidates import verify_candidate_execution_certificate
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _git_state, _Progress, _semantic_digest
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet, ValidatedLogicalPlan
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility import audit_source_freeze
from trustaero.validator.service import validate

BTS_MULTIJOIN_FORMAL_LABEL = "bts_multijoin_formal_development_partition_v1"


@dataclass(frozen=True, slots=True)
class BtsMultiJoinFormalConfig:
    results_dir: str
    sample_rows: int
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    absolute_half_drift_limit: float
    paired_ratio_half_drift_limit: float
    paired_ratio_outlier_fraction_limit: float
    tie_threshold_fraction: float
    query_family_protocol_sha256: str
    semantic_smoke_sha256: str
    full_month: bool = True
    require_clean_git: bool = True
    scientific_label: str = BTS_MULTIJOIN_FORMAL_LABEL
    paper_performance_evidence: bool = True
    heldout_optimizer_evidence: bool = False

    def __post_init__(self) -> None:
        if (
            not self.results_dir
            or self.sample_rows != 547_271
            or not self.full_month
            or not self.require_clean_git
            or self.scientific_label != BTS_MULTIJOIN_FORMAL_LABEL
            or not self.paper_performance_evidence
        ):
            raise ValueError("formal BTS multi-Join scope is invalid")
        if self.heldout_optimizer_evidence:
            raise ValueError("the January multi-Join is not optimizer holdout evidence")
        if self.warmup_blocks < 0 or self.measured_blocks < 48:
            raise ValueError("formal BTS multi-Join needs at least 48 measured blocks")
        if self.measured_blocks % 24:
            raise ValueError("four-candidate measured blocks must cover all 24 permutations")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 128:
            raise ValueError("BTS multi-Join DuckDB controls are invalid")
        for value in (
            self.absolute_half_drift_limit,
            self.paired_ratio_half_drift_limit,
            self.paired_ratio_outlier_fraction_limit,
            self.tie_threshold_fraction,
        ):
            if not 0.0 <= value < 1.0:
                raise ValueError("BTS multi-Join stability limits must be in [0, 1)")
        for digest in (self.query_family_protocol_sha256, self.semantic_smoke_sha256):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("BTS multi-Join bindings must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class BtsMultiJoinTiming:
    block_index: int
    block_id: str
    permutation_id: str
    order_position: int
    candidate_id: str
    started_at_utc: str
    client_materialization_latency_ms: float
    process_cpu_time_ms: float
    output_row_count: int
    semantic_result_digest: str


def load_bts_multijoin_formal_config(path: Path | str) -> BtsMultiJoinFormalConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("BTS multi-Join config must contain an object")
    return BtsMultiJoinFormalConfig(
        results_dir=str(payload["results_dir"]),
        sample_rows=int(payload["sample_rows"]),
        warmup_blocks=int(payload["warmup_blocks"]),
        measured_blocks=int(payload["measured_blocks"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        absolute_half_drift_limit=float(payload["absolute_half_drift_limit"]),
        paired_ratio_half_drift_limit=float(payload["paired_ratio_half_drift_limit"]),
        paired_ratio_outlier_fraction_limit=float(payload["paired_ratio_outlier_fraction_limit"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        query_family_protocol_sha256=str(payload["query_family_protocol_sha256"]),
        semantic_smoke_sha256=str(payload["semantic_smoke_sha256"]),
        full_month=bool(payload["full_month"]),
        require_clean_git=bool(payload["require_clean_git"]),
        scientific_label=str(payload["scientific_label"]),
        paper_performance_evidence=bool(payload["paper_performance_evidence"]),
        heldout_optimizer_evidence=bool(payload["heldout_optimizer_evidence"]),
    )


def _environment(config: BtsMultiJoinFormalConfig, commit: str) -> dict[str, Any]:
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
        "git_dirty": False,
        "packages": packages,
        "duckdb_threads": config.duckdb_threads,
        "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
        "gpu_acceleration": False,
        "cache_protocol": "hot_same_duckdb_connection",
    }


def _stage_statistics(connection: Any) -> dict[str, int | float]:
    row = connection.execute(
        """
        WITH governed AS (
          SELECT * FROM trust_bts_mj_flights
          WHERE FlightDate >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00'
            AND FlightDate < TIMESTAMPTZ '2024-01-22 00:00:00+00:00'
            AND Distance >= 750.0 AND Cancelled = false
        ), origin_joined AS (
          SELECT g.* FROM governed g JOIN trust_bts_mj_airports a
            ON g.OriginAirportID = a.airport_id
        ), carrier_joined AS (
          SELECT o.* FROM origin_joined o JOIN trust_bts_mj_carriers c
            ON o.DOT_ID_Reporting_Airline = c.carrier_id
        )
        SELECT
          (SELECT COUNT(*) FROM trust_bts_mj_flights),
          (SELECT COUNT(*) FROM governed),
          (SELECT COUNT(*) FROM origin_joined),
          (SELECT COUNT(*) FROM carrier_joined)
        """
    ).fetchone()
    if row is None:
        raise GovernedRealDataSmokeError("BTS multi-Join stage statistics are missing")
    input_rows, governed_rows, origin_rows, carrier_rows = map(int, row)
    return {
        "input_rows": input_rows,
        "governed_rows": governed_rows,
        "governed_selectivity": governed_rows / input_rows,
        "origin_join_rows": origin_rows,
        "origin_join_match_rate": origin_rows / governed_rows,
        "carrier_join_rows": carrier_rows,
        "carrier_join_match_rate": carrier_rows / origin_rows,
    }


def _write_csv(path: Path, rows: list[BtsMultiJoinTiming]) -> None:
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(BtsMultiJoinTiming.__annotations__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    os.replace(temporary, path)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def run_bts_multijoin_formal(
    config: BtsMultiJoinFormalConfig,
    *,
    project_root: Path,
    show_progress: bool = False,
) -> Path:
    """Run all four approved routes under the frozen paired protocol."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise GovernedRealDataSmokeError("DuckDB is required for BTS multi-Join") from exc
    root = project_root.resolve()
    for path, expected in (
        (
            root / "experiments/configs/real_data_query_families_v1.json",
            config.query_family_protocol_sha256,
        ),
        (
            root / "data/manifests/processed/bts-multijoin-semantic-smoke.json",
            config.semantic_smoke_sha256,
        ),
    ):
        if not path.is_file() or sha256_file(path) != expected:
            raise GovernedRealDataSmokeError(f"Frozen BTS multi-Join binding changed: {path}")
    if audit_source_freeze(root).status != "READY":
        raise GovernedRealDataSmokeError("formal BTS multi-Join requires source READY")
    commit, dirty = _git_state(root)
    if dirty:
        raise GovernedRealDataSmokeError("formal BTS multi-Join requires a clean worktree")
    artifacts = verify_bts_multijoin_full_month_artifacts(root / "data")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / config.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(run_dir / "config.json", asdict(config))
    _atomic_json(run_dir / "environment.json", _environment(config, commit))
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})

    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_multijoin_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_multijoin_policy.json"))
    response = validate(_load_json(examples / "plans/bts_natural_multijoin.json"), policy, catalog)
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("frozen BTS multi-Join no longer validates")
    logical: ValidatedLogicalPlan = response.validated_plan
    candidates = generate_duckdb_candidates(logical, materialization_targets=BTS_MULTIJOIN_TARGETS)
    if len(candidates) != 4:
        raise GovernedRealDataSmokeError("BTS multi-Join candidate space changed")

    connection = duckdb.connect()
    compiled: dict[str, CompiledQuery] = {}
    plans: dict[str, dict[str, Any]] = {}
    timings: list[BtsMultiJoinTiming] = []
    fingerprints: set[str] = set()
    expected_digest: str | None = None
    progress = _Progress(4 + 4 * (config.warmup_blocks + config.measured_blocks), show_progress)
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb-bts-multijoin-formal"
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(temp_dir)}")
        bindings = _create_bts_multijoin_views(
            connection, root / "data", sample_rows=config.sample_rows, full_month=True
        )
        stage = _stage_statistics(connection)
        for candidate in candidates:
            candidate_id = candidate.strategy.strategy_id
            query = compile_approved_physical_plan(logical, candidate, catalog, bindings)
            execution = execute_with_connection(query, connection)
            digest = _semantic_digest(execution.columns, execution.rows)
            if expected_digest is None:
                expected_digest = digest
            elif digest != expected_digest:
                raise GovernedRealDataSmokeError("BTS multi-Join candidate outputs differ")
            certificate = verify_candidate_execution_certificate(
                logical,
                candidate,
                execution,
                execution_id=f"bts-multijoin-formal-{run_id}-{candidate_id}",
            )
            observation = observe_duckdb_plan(connection, query.sql, query.parameters, analyze=True)
            if observation.fingerprint in fingerprints:
                raise GovernedRealDataSmokeError("BTS multi-Join plans collapsed")
            fingerprints.add(observation.fingerprint)
            compiled[candidate_id] = query
            plans[candidate_id] = {
                "physical_plan_id": candidate.physical_plan_id,
                "duckdb_plan_fingerprint": observation.fingerprint,
                "duckdb_operator_names": list(observation.operator_names),
                "actual_cardinalities": list(observation.actual_cardinalities),
                "rows_scanned": list(observation.rows_scanned),
                "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
                "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
                "certificate_status": certificate,
                "raw_rows_exposed_to_join": 0,
                "raw_rows_materialized": 0,
            }
            progress.advance(f"preflight {candidate_id}")
        candidate_ids = tuple(compiled)
        warmup_orders = complete_permutation_orders(
            candidate_ids, config.warmup_blocks, seed=config.order_seed
        )
        measured_orders = complete_permutation_orders(
            candidate_ids, config.measured_blocks, seed=config.order_seed + 1
        )
        schedule = [(False, index, order) for index, order in enumerate(warmup_orders)] + [
            (True, index, order) for index, order in enumerate(measured_orders)
        ]
        for measured, block_index, order in schedule:
            block_id = f"bts-multijoin-block-{block_index:03d}"
            permutation_id = " -> ".join(order)
            for position, candidate_id in enumerate(order):
                started_at = datetime.now(UTC).isoformat()
                cpu_started = time.process_time_ns()
                started = time.perf_counter_ns()
                execution = execute_with_connection(compiled[candidate_id], connection)
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
                digest = _semantic_digest(execution.columns, execution.rows)
                if digest != expected_digest:
                    raise GovernedRealDataSmokeError("BTS multi-Join timed result changed")
                if measured:
                    timings.append(
                        BtsMultiJoinTiming(
                            block_index,
                            block_id,
                            permutation_id,
                            position,
                            candidate_id,
                            started_at,
                            latency_ms,
                            cpu_ms,
                            execution.row_count,
                            digest,
                        )
                    )
                progress.advance(f"{'measure' if measured else 'warmup'} {candidate_id}")
            _atomic_json(
                run_dir / "progress.json",
                {
                    "completed_blocks": block_index + 1,
                    "phase": "measured" if measured else "warmup",
                    "updated_at_utc": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        connection.close()

    by_candidate = {
        candidate_id: [
            row.client_materialization_latency_ms
            for row in timings
            if row.candidate_id == candidate_id
        ]
        for candidate_id in compiled
    }
    summaries = {
        candidate_id: {
            "runs": len(values),
            "median_ms": statistics.median(values),
            "p95_ms": _percentile(values, 0.95),
            "min_ms": min(values),
            "max_ms": max(values),
            **plans[candidate_id],
        }
        for candidate_id, values in by_candidate.items()
    }
    _write_csv(run_dir / "measurements.csv", timings)
    _atomic_json(
        run_dir / "summary.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": "PASS",
            "scientific_label": config.scientific_label,
            "paper_performance_evidence": True,
            "heldout_optimizer_evidence": False,
            "optimizer_selection_evaluated": False,
            "cache_protocol": "hot_same_duckdb_connection",
            "sample_rows": config.sample_rows,
            "candidate_count": len(compiled),
            "distinct_duckdb_plan_count": len(fingerprints),
            "verified_execution_artifacts": [asdict(item) for item in artifacts],
            "stage_statistics": stage,
            "candidate_summaries": summaries,
            "measurement_count": len(timings),
        },
    )
    return run_dir


def _half_drift(values: list[float]) -> float:
    midpoint = len(values) // 2
    first = statistics.median(values[:midpoint])
    second = statistics.median(values[midpoint:])
    return abs(second / first - 1.0)


def _outlier_fraction(values: list[float]) -> float:
    center = statistics.median(values)
    deviations = [abs(value - center) for value in values]
    threshold = max(3.0 * statistics.median(deviations), 0.15)
    return sum(value > threshold for value in deviations) / len(values)


def analyze_bts_multijoin_formal(run_dir: Path) -> dict[str, Any]:
    """Apply the predeclared four-candidate balance and stability gates."""

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    candidate_ids = tuple(summary["candidate_summaries"])
    by_candidate: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_block: dict[int, dict[str, float]] = defaultdict(dict)
    permutation_by_block: dict[int, str] = {}
    for row in rows:
        candidate_id = row["candidate_id"]
        block = int(row["block_index"])
        by_candidate[candidate_id].append(row)
        by_block[block][candidate_id] = float(row["client_materialization_latency_ms"])
        permutation_by_block[block] = row["permutation_id"]
    fused_id = "fused"
    ratios = {
        candidate_id: [
            values[candidate_id] / values[fused_id] for _, values in sorted(by_block.items())
        ]
        for candidate_id in candidate_ids
        if candidate_id != fused_id
    }
    absolute_drift = {
        candidate_id: _half_drift(
            [
                float(row["client_materialization_latency_ms"])
                for row in sorted(items, key=lambda item: int(item["block_index"]))
            ]
        )
        for candidate_id, items in by_candidate.items()
    }
    ratio_drift = {candidate_id: _half_drift(values) for candidate_id, values in ratios.items()}
    outliers = {candidate_id: _outlier_fraction(values) for candidate_id, values in ratios.items()}
    position_counts = {
        candidate_id: Counter(int(row["order_position"]) for row in items)
        for candidate_id, items in by_candidate.items()
    }
    permutation_counts = Counter(permutation_by_block.values())
    integrity = {
        "clean_source_recorded": environment.get("git_dirty") is False,
        "candidate_space_complete": len(candidate_ids) == 4
        and int(summary["distinct_duckdb_plan_count"]) == 4,
        "measurements_complete": len(rows) == 4 * int(config["measured_blocks"]),
        "all_24_permutations_balanced": len(permutation_counts) == 24
        and set(permutation_counts.values()) == {int(config["measured_blocks"]) // 24},
        "all_positions_balanced": all(
            set(counts) == {0, 1, 2, 3} and len(set(counts.values())) == 1
            for counts in position_counts.values()
        ),
        "artifacts_verified": len(summary["verified_execution_artifacts"]) == 3,
        "certificates_partial": all(
            item["certificate_status"] == "PARTIAL"
            for item in summary["candidate_summaries"].values()
        ),
        "resources_observed": all(
            int(item["peak_buffer_memory_bytes"]) > 0
            and int(item["peak_temp_directory_bytes"]) >= 0
            for item in summary["candidate_summaries"].values()
        ),
    }
    stability = {
        "absolute_half_drift": max(absolute_drift.values())
        <= float(config["absolute_half_drift_limit"]),
        "paired_ratio_half_drift": max(ratio_drift.values())
        <= float(config["paired_ratio_half_drift_limit"]),
        "paired_ratio_outlier_fraction": max(outliers.values())
        <= float(config["paired_ratio_outlier_fraction_limit"]),
    }
    normalized = {"fused": 1.0, **{key: statistics.median(value) for key, value in ratios.items()}}
    best = min(normalized.values())
    tie = float(config["tie_threshold_fraction"])
    oracle_set = sorted(
        candidate_id for candidate_id, ratio in normalized.items() if ratio <= best * (1.0 + tie)
    )
    passed = all(integrity.values()) and all(stability.values())
    payload = {
        "schema_version": 1,
        "run_id": summary["run_id"],
        "status": "PASS" if passed else "FAIL",
        "scientific_label": summary["scientific_label"],
        "paper_performance_evidence": True,
        "heldout_optimizer_evidence": False,
        "formal_paper_experiment_authorized": passed,
        "integrity_gates": integrity,
        "stability_gates": stability,
        "absolute_half_drift_by_candidate": absolute_drift,
        "paired_ratio_half_drift_by_candidate": ratio_drift,
        "paired_ratio_outlier_fraction_by_candidate": outliers,
        "median_candidate_over_fused_ratio": normalized,
        "diagnostic_oracle_set_within_tie_band": oracle_set,
        "optimizer_selection_evaluated": False,
        "scientific_boundary": (
            "This full-January multi-Join run is method-level development evidence. "
            "It computes a diagnostic Oracle after timing all candidates and is not "
            "holdout evidence."
        ),
    }
    _atomic_json(run_dir / "acceptance.json", payload)
    lines = [
        "# BTS full-month natural multi-Join measurement",
        "",
        f"Status: **{payload['status']}**",
        "",
        "| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for candidate_id, values in summary["candidate_summaries"].items():
        lines.append(
            f"| {candidate_id} | {values['median_ms']:.3f} | {values['p95_ms']:.3f} | "
            f"{values['peak_buffer_memory_bytes'] / 1048576:.2f} |"
        )
    lines.extend(
        [
            "",
            "- Four-candidate 24-permutation balance: "
            f"`{integrity['all_24_permutations_balanced']}`",
            f"- Stability gates: `{stability}`",
            f"- Paired 3% Oracle set: `{oracle_set}`",
            "- Optimizer selection evaluated: `False`.",
            "",
        ]
    )
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return payload
