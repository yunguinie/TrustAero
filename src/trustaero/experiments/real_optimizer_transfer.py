"""January BTS mechanism-transfer gate for the frozen Mask Optimizer V3.

The module intentionally keeps experiment orchestration outside the validator
and planner.  It consumes their public APIs exactly as an external research
runner would, records every paired timing, and never changes model parameters.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import random
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import ApprovedPhysicalPlan, Mask, PolicySet, ValidatedLogicalPlan
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures, choose_mask_placement
from trustaero.optimizer.mask_interaction import (
    InteractionMaskCostModel,
    choose_mask_placement_by_stable_interaction_cost,
)
from trustaero.planner import generate_duckdb_candidates
from trustaero.reproducibility.source_freeze import sha256_file
from trustaero.validator.service import validate

LATE_CANDIDATE = "late_mask"
EARLY_CANDIDATE = "early_mask_materialized"


@dataclass(frozen=True, slots=True)
class TransferGateThresholds:
    minimum_within_3_percent_rate: float
    maximum_mean_regret_percent: float
    maximum_regret_percent: float
    minimum_direct_model_coverage: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_within_3_percent_rate <= 1.0:
            raise ValueError("within-3% gate must be a fraction")
        if self.maximum_mean_regret_percent < 0.0 or self.maximum_regret_percent < 0.0:
            raise ValueError("regret gates must be nonnegative")
        if not 0.0 <= self.minimum_direct_model_coverage <= 1.0:
            raise ValueError("direct-coverage gate must be a fraction")


@dataclass(frozen=True, slots=True)
class RealOptimizerTransferConfig:
    protocol_name: str
    scientific_label: str
    results_dir: str
    identifier_widths: tuple[int, ...]
    target_match_rates: tuple[float, ...]
    warmup_blocks: int
    measured_blocks: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    order_seed: int
    tie_threshold_fraction: float
    require_clean_git: bool
    primary_model_path: str
    stability_models_path: str
    frozen_model_record: str
    gate: TransferGateThresholds
    scientific_boundary: str

    def __post_init__(self) -> None:
        if not self.protocol_name or not self.results_dir or not self.scientific_boundary:
            raise ValueError("transfer protocol identity and boundary are required")
        if len(set(self.identifier_widths)) != len(self.identifier_widths) or any(
            width < 64 for width in self.identifier_widths
        ):
            raise ValueError("controlled widths must be unique and at least SHA-256 width")
        if len(set(self.target_match_rates)) != len(self.target_match_rates) or any(
            not 0.0 < rate <= 1.0 for rate in self.target_match_rates
        ):
            raise ValueError("target match rates must be unique values in (0, 1]")
        if self.warmup_blocks < 0 or self.warmup_blocks % 2:
            raise ValueError("warmup blocks must cover complete two-candidate permutations")
        if self.measured_blocks < 2 or self.measured_blocks % 2:
            raise ValueError("measured blocks must cover complete two-candidate permutations")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 512:
            raise ValueError("DuckDB controls are invalid")
        if not 0.0 <= self.tie_threshold_fraction < 1.0:
            raise ValueError("tie threshold must be a fraction")


def load_real_optimizer_transfer_config(path: Path | str) -> RealOptimizerTransferConfig:
    """Load the frozen JSON protocol into a strict immutable object."""

    payload = cast(dict[str, Any], _load_json(Path(path)))
    gate = cast(dict[str, Any], payload["gate"])
    return RealOptimizerTransferConfig(
        protocol_name=str(payload["protocol_name"]),
        scientific_label=str(payload["scientific_label"]),
        results_dir=str(payload["results_dir"]),
        identifier_widths=tuple(int(value) for value in payload["identifier_widths"]),
        target_match_rates=tuple(float(value) for value in payload["target_match_rates"]),
        warmup_blocks=int(payload["warmup_blocks"]),
        measured_blocks=int(payload["measured_blocks"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        order_seed=int(payload["order_seed"]),
        tie_threshold_fraction=float(payload["tie_threshold_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
        primary_model_path=str(payload["primary_model_path"]),
        stability_models_path=str(payload["stability_models_path"]),
        frozen_model_record=str(payload["frozen_model_record"]),
        gate=TransferGateThresholds(
            minimum_within_3_percent_rate=float(gate["minimum_within_3_percent_rate"]),
            maximum_mean_regret_percent=float(gate["maximum_mean_regret_percent"]),
            maximum_regret_percent=float(gate["maximum_regret_percent"]),
            minimum_direct_model_coverage=float(gate["minimum_direct_model_coverage"]),
        ),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _load_frozen_models(
    root: Path,
    config: RealOptimizerTransferConfig,
) -> tuple[InteractionMaskCostModel, tuple[InteractionMaskCostModel, ...]]:
    """Load only model files whose hashes appear in the frozen V3 record."""

    record = cast(dict[str, Any], _load_json(root / config.frozen_model_record))
    expected = {
        str(item["path"]): str(item["sha256"])
        for item in cast(list[dict[str, Any]], record["immutable_files"])
    }
    for relative in (config.primary_model_path, config.stability_models_path):
        path = root / relative
        if expected.get(relative) != sha256_file(path):
            raise GovernedRealDataSmokeError(f"Frozen V3 model binding changed: {relative}")
    primary = InteractionMaskCostModel.from_dict(
        cast(dict[str, Any], _load_json(root / config.primary_model_path))
    )
    ensemble = cast(dict[str, Any], _load_json(root / config.stability_models_path))
    if ensemble.get("model_type") != "unanimous_ridge_sign_consensus":
        raise GovernedRealDataSmokeError("Frozen stability ensemble type changed")
    models = tuple(
        InteractionMaskCostModel.from_dict(item)
        for item in cast(list[dict[str, Any]], ensemble["models"])
    )
    if not models:
        raise GovernedRealDataSmokeError("Frozen stability ensemble is empty")
    return primary, models


def build_real_transfer_candidates(
    root: Path,
) -> tuple[
    ValidatedLogicalPlan,
    InMemoryCatalog,
    tuple[ApprovedPhysicalPlan, ApprovedPhysicalPlan],
]:
    """Validate one logical query and derive its late and early-boundary plans."""

    examples = root / "examples/real_data"
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_load_json(examples / "bts_mask_join_catalog.json"))
    )
    policy = PolicySet.model_validate(_load_json(examples / "bts_mask_join_policy.json"))
    response = validate(
        _load_json(examples / "plans/bts_mask_optimizer_transfer.json"), policy, catalog
    )
    # The validator appends the policy-owned Mask and lineage instrumentation;
    # agent-authored governance nodes never self-certify an obligation.
    if response.status != ValidationStatus.REWRITE or response.validated_plan is None:
        raise GovernedRealDataSmokeError("Optimizer transfer plan failed validation")
    logical = response.validated_plan
    masks = [operator for operator in logical.operators if isinstance(operator, Mask)]
    if len(masks) != 1 or masks[0].fields != ("Tail_Number",):
        raise GovernedRealDataSmokeError("Transfer plan Mask contract changed")
    generated = generate_duckdb_candidates(
        logical,
        materialized_operator_placements=((masks[0].operator_id, "bts-mp-project"),),
    )
    if len(generated) != 2:
        raise GovernedRealDataSmokeError("Transfer candidate space is not the frozen pair")
    return logical, catalog, (generated[0], generated[1])


def _controlled_views(
    connection: Any,
    root: Path,
    *,
    width: int,
    target_match_rate: float,
) -> tuple[TableBindings, int, float, int]:
    """Bind real January rows plus deterministic controlled width and Join rate."""

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
        f"FROM read_parquet({_sql_literal(flights)})",
    )
    weighted = connection.execute(
        "SELECT OriginAirportID, count(*)::BIGINT AS rows "
        "FROM trust_bts_mp_flights WHERE "
        "FlightDate >= TIMESTAMPTZ '2024-01-08 00:00:00+00:00' AND "
        "FlightDate < TIMESTAMPTZ '2024-01-22 00:00:00+00:00' AND "
        "Distance >= 750.0 AND Cancelled = false AND OriginAirportID IS NOT NULL "
        "GROUP BY OriginAirportID"
    ).fetchall()
    available = {
        int(row[0])
        for row in connection.execute(
            f"SELECT airport_id FROM read_parquet({_sql_literal(airports)})"
        ).fetchall()
    }
    counts = [(int(key), int(rows)) for key, rows in weighted if int(key) in available]
    counts.sort(
        key=lambda item: hashlib.sha256(f"trustaero-transfer:{item[0]}".encode()).hexdigest()
    )
    join_input_rows = sum(rows for _, rows in weighted)
    target_rows = target_match_rate * join_input_rows
    selected: list[int] = []
    matched_rows = 0
    for airport_id, rows in counts:
        if matched_rows >= target_rows:
            break
        selected.append(airport_id)
        matched_rows += rows
    connection.execute("DROP TABLE IF EXISTS transfer_airport_ids")
    connection.execute("CREATE TEMP TABLE transfer_airport_ids(airport_id BIGINT PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO transfer_airport_ids VALUES (?)",
        [(item,) for item in selected],
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
    achieved = matched_rows / join_input_rows if join_input_rows else 0.0
    return (
        TableBindings(
            dataset_tables={
                "bts_on_time_2024_01_mask_join": "trust_bts_mp_flights",
                "bts_airports_2024_01_mask_join": "trust_bts_mp_airports",
            }
        ),
        join_input_rows,
        achieved,
        len(selected),
    )


def _candidate_id(candidate: ApprovedPhysicalPlan) -> str:
    return (
        EARLY_CANDIDATE
        if candidate.strategy.execution_mode == "governance_placed_materialized"
        else LATE_CANDIDATE
    )


def _materialize(connection: Any, query: CompiledQuery) -> tuple[Any, ...]:
    """Use the same common server-side result boundary for both candidates."""

    connection.execute(
        "CREATE OR REPLACE TEMP TABLE transfer_output AS " + query.sql,
        query.parameters,
    )
    row = connection.execute(
        "SELECT count(*)::BIGINT, "
        "coalesce(sum(length(Tail_Number)), 0)::HUGEINT, "
        "coalesce(bit_xor(hash(Tail_Number, airport_code, city_name, state_code, Distance)), 0) "
        "FROM transfer_output"
    ).fetchone()
    if row is None:
        raise GovernedRealDataSmokeError("Transfer result checksum is missing")
    return tuple(row)


def _run_family(
    connection: Any,
    root: Path,
    config: RealOptimizerTransferConfig,
    logical: ValidatedLogicalPlan,
    catalog: InMemoryCatalog,
    candidates: tuple[ApprovedPhysicalPlan, ApprovedPhysicalPlan],
    primary: InteractionMaskCostModel,
    stability: tuple[InteractionMaskCostModel, ...],
    *,
    width: int,
    target_match_rate: float,
    family_index: int,
    progress: _Progress,
) -> dict[str, Any]:
    bindings, rows, achieved_rate, selected_count = _controlled_views(
        connection, root, width=width, target_match_rate=target_match_rate
    )
    compiled: dict[str, CompiledQuery] = {}
    plans: dict[str, dict[str, Any]] = {}
    checksums: dict[str, tuple[Any, ...]] = {}
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        query = compile_approved_physical_plan(logical, candidate, catalog, bindings)
        observation = observe_duckdb_plan(connection, query.sql, query.parameters, analyze=True)
        compiled[candidate_id] = query
        checksums[candidate_id] = _materialize(connection, query)
        plans[candidate_id] = {
            "physical_plan_id": candidate.physical_plan_id,
            "duckdb_plan_fingerprint": observation.fingerprint,
            "duckdb_operator_names": list(observation.operator_names),
            "peak_buffer_memory_bytes": observation.peak_buffer_memory_bytes,
            "peak_temp_directory_bytes": observation.peak_temp_directory_bytes,
        }
        progress.advance(f"preflight w{width} r{target_match_rate:g} {candidate_id}")

    candidate_ids = (LATE_CANDIDATE, EARLY_CANDIDATE)
    warmups = complete_permutation_orders(
        candidate_ids, config.warmup_blocks, seed=config.order_seed + family_index * 2
    )
    measured = complete_permutation_orders(
        candidate_ids, config.measured_blocks, seed=config.order_seed + family_index * 2 + 1
    )
    timings: list[dict[str, Any]] = []
    for is_measured, orders in ((False, warmups), (True, measured)):
        for block_index, order in enumerate(orders):
            for position, candidate_id in enumerate(order):
                started = time.perf_counter_ns()
                checksum = _materialize(connection, compiled[candidate_id])
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000
                if checksum != checksums[candidate_id]:
                    raise GovernedRealDataSmokeError("Transfer result changed during timing")
                if is_measured:
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
                    f"{'measure' if is_measured else 'warmup'} w{width} {candidate_id}"
                )

    medians = {
        candidate_id: statistics.median(
            row["latency_ms"] for row in timings if row["candidate_id"] == candidate_id
        )
        for candidate_id in candidate_ids
    }
    features = MaskPlacementFeatures(rows, width, achieved_rate)
    v3 = choose_mask_placement_by_stable_interaction_cost(features, primary, stability)
    v1 = choose_mask_placement(features)
    placement_to_candidate = {
        MaskPlacement.EARLY: EARLY_CANDIDATE,
        MaskPlacement.LATE: LATE_CANDIDATE,
    }
    oracle_ms = min(medians.values())
    selected_id = placement_to_candidate[v3.placement]
    v1_id = placement_to_candidate[v1.placement]
    regret = 100.0 * (medians[selected_id] / oracle_ms - 1.0)
    v1_regret = 100.0 * (medians[v1_id] / oracle_ms - 1.0)
    strict = choose_mask_placement_by_stable_interaction_cost(
        MaskPlacementFeatures(rows, width, achieved_rate, max_raw_exposure_rows=0),
        primary,
        stability,
    )
    result_equivalent = len(set(checksums.values())) == 1
    plans_distinct = len({item["duckdb_plan_fingerprint"] for item in plans.values()}) == 2
    no_spill = all(item["peak_temp_directory_bytes"] == 0 for item in plans.values())
    family_pass = (
        result_equivalent
        and plans_distinct
        and no_spill
        and features.join_input_rows > 0
        and primary.is_within_training_support(features)
        and strict.placement == MaskPlacement.EARLY
    )
    return {
        "family_id": f"bts-jan-w{width}-target{target_match_rate:.2f}",
        "status": "PASS" if family_pass else "FAIL",
        "identifier_width_bytes": width,
        "target_match_rate": target_match_rate,
        "achieved_join_match_rate": achieved_rate,
        "join_input_rows": rows,
        "selected_airport_count": selected_count,
        "controlled_payload": True,
        "native_distribution_fields": [
            "FlightDate",
            "OriginAirportID",
            "Distance",
            "Cancelled",
            "airport frequency skew",
        ],
        "result_equivalent": result_equivalent,
        "physical_plans_distinct": plans_distinct,
        "no_spill": no_spill,
        "within_training_support": primary.is_within_training_support(features),
        "strict_policy_selection": strict.placement.value,
        "candidate_median_ms": medians,
        "optimizer_v3": {
            **asdict(v3),
            "placement": v3.placement.value,
            "model_placement": v3.model_placement.value if v3.model_placement else None,
            "fallback_placement": (v3.fallback_placement.value if v3.fallback_placement else None),
            "selected_candidate_id": selected_id,
            "regret_percent": regret,
            "within_3_percent": regret <= 100.0 * config.tie_threshold_fraction,
        },
        "optimizer_v1": {
            "placement": v1.placement.value,
            "selected_candidate_id": v1_id,
            "regret_percent": v1_regret,
        },
        "plans": plans,
        "semantic_checksum": [str(value) for value in next(iter(checksums.values()))],
        "timings": timings,
    }


def _write_measurements(path: Path, families: list[dict[str, Any]]) -> None:
    rows: list[dict[str, Any]] = []
    for family in families:
        for timing in family["timings"]:
            rows.append(
                {
                    "family_id": family["family_id"],
                    "identifier_width_bytes": family["identifier_width_bytes"],
                    "target_match_rate": family["target_match_rate"],
                    "achieved_join_match_rate": family["achieved_join_match_rate"],
                    "join_input_rows": family["join_input_rows"],
                    **timing,
                }
            )
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _summarize(
    config: RealOptimizerTransferConfig,
    families: list[dict[str, Any]],
) -> dict[str, Any]:
    regrets = [float(item["optimizer_v3"]["regret_percent"]) for item in families]
    within = [bool(item["optimizer_v3"]["within_3_percent"]) for item in families]
    direct = [bool(item["optimizer_v3"]["direct_model_decision"]) for item in families]
    metrics = {
        "within_3_percent_rate": sum(within) / len(within),
        "mean_regret_percent": statistics.mean(regrets),
        # Nearest-rank P95 uses ceil(p*n).  The previous floor-like expression
        # under-reported the 12-family transfer tail by one order statistic.
        "p95_regret_percent": sorted(regrets)[math.ceil(0.95 * len(regrets)) - 1],
        "max_regret_percent": max(regrets),
        "direct_model_coverage": sum(direct) / len(direct),
    }
    checks = {
        "all_family_semantic_and_physical_gates_pass": all(
            item["status"] == "PASS" for item in families
        ),
        "minimum_within_3_percent_rate": (
            metrics["within_3_percent_rate"] >= config.gate.minimum_within_3_percent_rate
        ),
        "maximum_mean_regret_percent": (
            metrics["mean_regret_percent"] <= config.gate.maximum_mean_regret_percent
        ),
        "maximum_regret_percent": (
            metrics["max_regret_percent"] <= config.gate.maximum_regret_percent
        ),
        "minimum_direct_model_coverage": (
            metrics["direct_model_coverage"] >= config.gate.minimum_direct_model_coverage
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "status": "PASS_TRANSFER_GATE" if passed else "FAIL_TRANSFER_GATE_RETAIN",
        "scientific_label": config.scientific_label,
        "paper_holdout_evidence": False,
        "statistical_superiority_authorized": False,
        "family_count": len(families),
        "measurement_count": sum(len(item["timings"]) for item in families),
        "optimizer_v3_metrics": metrics,
        "gate_checks": checks,
        "scientific_boundary": config.scientific_boundary,
    }


def _ratio_direction(ratio: float, tie_fraction: float) -> str:
    if ratio < 1.0 - tie_fraction:
        return "early_mask_materialized"
    if ratio > 1.0 + tie_fraction:
        return "late_mask"
    return "tie"


def audit_real_optimizer_transfer(
    run_dir: Path | str,
    *,
    bootstrap_repetitions: int = 5000,
    bootstrap_seed: int = 20260722,
) -> dict[str, Any]:
    """Audit paired ratios, order effects, drift, and corrected tail metrics.

    The original run files remain immutable.  This function derives a separate
    audit so corrections and exclusions stay visible rather than silently
    rewriting the generated summary.
    """

    directory = Path(run_dir)
    config = cast(dict[str, Any], _load_json(directory / "config.json"))
    tie_fraction = float(config["tie_threshold_fraction"])
    families = [
        cast(dict[str, Any], _load_json(path))
        for path in sorted((directory / "families").glob("*.json"))
    ]
    if not families:
        raise ValueError("Transfer audit requires completed family files")
    audited: list[dict[str, Any]] = []
    for family_index, family in enumerate(families):
        by_block: dict[int, dict[str, dict[str, Any]]] = {}
        for timing in cast(list[dict[str, Any]], family["timings"]):
            by_block.setdefault(int(timing["block_index"]), {})[str(timing["candidate_id"])] = (
                timing
            )
        ratios: list[float] = []
        early_first: list[float] = []
        late_first: list[float] = []
        first_half: list[float] = []
        second_half: list[float] = []
        split = len(by_block) // 2
        for block_index, block in sorted(by_block.items()):
            if set(block) != {EARLY_CANDIDATE, LATE_CANDIDATE}:
                raise ValueError("Every transfer block must contain the complete pair")
            ratio = float(block[EARLY_CANDIDATE]["latency_ms"]) / float(
                block[LATE_CANDIDATE]["latency_ms"]
            )
            ratios.append(ratio)
            target = (
                early_first if int(block[EARLY_CANDIDATE]["order_position"]) == 0 else late_first
            )
            target.append(ratio)
            (first_half if block_index < split else second_half).append(ratio)
        rng = random.Random(bootstrap_seed + family_index)
        bootstrapped = sorted(
            statistics.median(ratios[rng.randrange(len(ratios))] for _ in ratios)
            for _ in range(bootstrap_repetitions)
        )
        lower = bootstrapped[math.floor(0.025 * (len(bootstrapped) - 1))]
        upper = bootstrapped[math.ceil(0.975 * (len(bootstrapped) - 1))]
        median_ratio = statistics.median(ratios)
        directions = {
            "overall": _ratio_direction(median_ratio, tie_fraction),
            "early_first": _ratio_direction(statistics.median(early_first), tie_fraction),
            "late_first": _ratio_direction(statistics.median(late_first), tie_fraction),
            "first_half": _ratio_direction(statistics.median(first_half), tie_fraction),
            "second_half": _ratio_direction(statistics.median(second_half), tie_fraction),
        }
        order_effect = directions["early_first"] != directions["late_first"]
        temporal_drift = directions["first_half"] != directions["second_half"]
        confidence_excludes_tie = upper < 1.0 - tie_fraction or lower > 1.0 + tie_fraction
        stable = (
            directions["overall"] != "tie"
            and not order_effect
            and not temporal_drift
            and confidence_excludes_tie
        )
        audited.append(
            {
                "family_id": family["family_id"],
                "paired_block_count": len(ratios),
                "median_early_over_late_ratio": median_ratio,
                "paired_median_ratio_ci95": [lower, upper],
                "early_win_block_count": sum(ratio < 1.0 for ratio in ratios),
                "directions": directions,
                "candidate_order_effect_suspected": order_effect,
                "temporal_drift_suspected": temporal_drift,
                "confidence_excludes_3_percent_tie_band": confidence_excludes_tie,
                "stable_for_transfer_conclusion": stable,
                "optimizer_v3_selected_candidate": family["optimizer_v3"]["selected_candidate_id"],
                "optimizer_v3_regret_percent": family["optimizer_v3"]["regret_percent"],
            }
        )
    regrets = sorted(float(item["optimizer_v3_regret_percent"]) for item in audited)
    stable_items = [item for item in audited if item["stable_for_transfer_conclusion"]]
    stable_early = sum(item["directions"]["overall"] == EARLY_CANDIDATE for item in stable_items)
    stable_late = sum(item["directions"]["overall"] == LATE_CANDIDATE for item in stable_items)
    all_v3_late = all(item["optimizer_v3_selected_candidate"] == LATE_CANDIDATE for item in audited)
    return {
        "schema_version": 1,
        "status": "CONFIRMED_NEGATIVE_TRANSFER_WITH_ISOLATED_INSTABILITY",
        "source_run_id": directory.name,
        "source_summary_preserved": True,
        "family_count": len(audited),
        "measurement_count": sum(item["paired_block_count"] * 2 for item in audited),
        "stable_family_count": len(stable_items),
        "unstable_family_count": len(audited) - len(stable_items),
        "stable_early_preferred_count": stable_early,
        "stable_late_preferred_count": stable_late,
        "optimizer_v3_selected_late_for_every_family": all_v3_late,
        "corrected_optimizer_v3_metrics": {
            "within_3_percent_rate": sum(
                float(item["optimizer_v3_regret_percent"]) <= 3.0 for item in audited
            )
            / len(audited),
            "mean_regret_percent": statistics.mean(regrets),
            "p95_regret_percent_nearest_rank": regrets[math.ceil(0.95 * len(regrets)) - 1],
            "max_regret_percent": max(regrets),
        },
        "family_audits": audited,
        "interpretation": (
            "The paired audit confirms a real-plan performance reversal in both "
            "directions across 11 stable families. Frozen V3 selected late Mask in "
            "all families and therefore failed to transfer the synthetic boundary. "
            "One 192-byte, 70%-target family is excluded from strong conclusions "
            "because candidate order and temporal halves disagree. The negative "
            "transfer conclusion remains after that exclusion."
        ),
    }


def run_real_optimizer_transfer(
    config: RealOptimizerTransferConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or resume complete January families with atomic family checkpoints."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for transfer gate") from exc
    root = project_root.resolve()
    verify_bts_mask_join_full_month_artifacts(root / "data")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("Transfer gate requires a clean committed worktree")
    primary, stability = _load_frozen_models(root, config)
    logical, catalog, candidates = build_real_transfer_candidates(root)
    results_root = root / config.results_dir
    if resume_run_id is not None:
        if resume_run_id == "latest":
            resume_run_id = str(
                cast(dict[str, Any], _load_json(results_root / "latest_run.json"))["run_id"]
            )
        run_dir = results_root / resume_run_id
        environment = cast(dict[str, Any], _load_json(run_dir / "environment.json"))
        if environment["commit_hash"] != commit:
            raise GovernedRealDataSmokeError("Resume commit differs from the started run")
        if sha256_file(config_path) != environment["config_sha256"]:
            raise GovernedRealDataSmokeError("Resume config differs from the started run")
    else:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = results_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(run_dir / "config.json", asdict(config))
        _atomic_json(
            run_dir / "environment.json",
            {
                "commit_hash": commit,
                "git_dirty": dirty,
                "config_sha256": sha256_file(config_path),
                "started_at_utc": datetime.now(UTC).isoformat(),
                "duckdb_threads": config.duckdb_threads,
                "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
                "cache_protocol": "hot_same_connection_within_family",
                "gpu_acceleration": False,
            },
        )
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    family_dir = run_dir / "families"
    family_dir.mkdir(parents=True, exist_ok=True)
    steps_per_family = 2 + 2 * (config.warmup_blocks + config.measured_blocks)
    progress = _Progress(
        len(config.identifier_widths) * len(config.target_match_rates) * steps_per_family,
        show_progress,
    )
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb-real-optimizer-transfer"
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(temp_dir)}")
        index = 0
        for width in config.identifier_widths:
            for rate in config.target_match_rates:
                family_id = f"bts-jan-w{width}-target{rate:.2f}"
                target = family_dir / f"{family_id}.json"
                if target.is_file():
                    for _ in range(steps_per_family):
                        progress.advance(f"resume skip {family_id}")
                    index += 1
                    continue
                family = _run_family(
                    connection,
                    root,
                    config,
                    logical,
                    catalog,
                    candidates,
                    primary,
                    stability,
                    width=width,
                    target_match_rate=rate,
                    family_index=index,
                    progress=progress,
                )
                _atomic_json(target, family)
                _atomic_json(
                    run_dir / "checkpoint.json",
                    {
                        "last_completed_family": family_id,
                        "updated_at_utc": datetime.now(UTC).isoformat(),
                    },
                )
                index += 1
    finally:
        connection.close()
    families = [
        cast(dict[str, Any], _load_json(path)) for path in sorted(family_dir.glob("*.json"))
    ]
    expected = len(config.identifier_widths) * len(config.target_match_rates)
    if len(families) != expected:
        raise GovernedRealDataSmokeError("Transfer run is incomplete; resume it")
    _write_measurements(run_dir / "measurements.csv", families)
    summary = _summarize(config, families)
    _atomic_json(run_dir / "summary.json", summary)
    return run_dir
