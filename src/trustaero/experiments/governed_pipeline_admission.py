"""Frozen winner-diversity admission for governed pipeline candidates."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
from trustaero.experiments.governed_checkpoint_reversal import (
    GovernedCheckpointUnit,
)
from trustaero.experiments.governed_pipeline_execution import (
    ExecutableGovernedPipeline,
    build_executable_governed_pipeline,
    execute_governed_pipeline,
    observe_governed_pipeline_plan,
)
from trustaero.experiments.paired_claims import assess_carryover
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_pipeline_space import (
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    GovernedPipelineStatistics,
    plan_governed_pipeline,
)

ADMISSION_CANDIDATE_IDS = (
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    JOIN_FIRST_MASKED_CHECKPOINT,
)


@dataclass(frozen=True, slots=True)
class GovernedPipelineAdmissionConfig:
    """Frozen workload grid, timing controls, and stop/go gates."""

    results_dir: str
    row_count: int
    identifier_widths: tuple[int, ...]
    policy_selectivities: tuple[float, ...]
    query_selectivities: tuple[float, ...]
    join_match_rates: tuple[float, ...]
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

    def __post_init__(self) -> None:
        dimensions = (
            self.identifier_widths,
            self.policy_selectivities,
            self.query_selectivities,
            self.join_match_rates,
            self.seeds,
        )
        if any(not values or len(values) != len(set(values)) for values in dimensions):
            raise ValueError("Pipeline admission dimensions must be nonempty and unique")
        if self.row_count <= 0 or len(self.seeds) < 3:
            raise ValueError("Pipeline admission requires rows and three seeds")
        if self.candidate_ids != ADMISSION_CANDIDATE_IDS:
            raise ValueError("Pipeline admission candidate set changed")
        if self.warmup_rounds_per_permutation < 1:
            raise ValueError("Pipeline admission requires balanced warmup")
        if self.measured_rounds_per_permutation < 5:
            raise ValueError("Pipeline admission requires five measured rounds")
        if any(not 0.0 < value <= 1.0 for value in self.join_match_rates):
            raise ValueError("Join match rates must be in (0, 1]")
        selectivities = self.policy_selectivities + self.query_selectivities
        if any(not 0.0 < value < 1.0 for value in selectivities):
            raise ValueError("Selectivities must be in (0, 1)")
        if not 0.0 < self.practical_tie_fraction < 1.0:
            raise ValueError("Practical tie fraction must be in (0, 1)")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("Confidence level must be in (0, 1)")
        if self.bootstrap_draws < 1_000:
            raise ValueError("Admission requires at least 1000 bootstrap draws")
        if not 0.0 <= self.minimum_conclusive_scenario_rate <= 1.0:
            raise ValueError("Conclusive scenario rate must be in [0, 1]")
        if not 0.0 < self.maximum_dominant_singleton_winner_fraction <= 1.0:
            raise ValueError("Dominant winner fraction must be in (0, 1]")
        if not 2 <= self.minimum_distinct_singleton_winners <= len(self.candidate_ids):
            raise ValueError("Admission requires at least two distinct performance winners")

    @property
    def measured_blocks_per_unit(self) -> int:
        return math.factorial(len(self.candidate_ids)) * self.measured_rounds_per_permutation


@dataclass(frozen=True, slots=True)
class GovernedPipelineAdmissionUnit:
    """One indivisible width-policy-query-match-seed workload."""

    row_count: int
    identifier_width: int
    policy_selectivity: float
    query_selectivity: float
    join_match_rate: float
    seed: int

    @property
    def scenario_id(self) -> str:
        return (
            f"n{self.row_count}-w{self.identifier_width}-"
            f"p{self.policy_selectivity}-q{self.query_selectivity}-"
            f"j{self.join_match_rate}"
        )

    @property
    def unit_id(self) -> str:
        return f"{self.scenario_id}-s{self.seed}"

    @property
    def checkpoint_unit(self) -> GovernedCheckpointUnit:
        return GovernedCheckpointUnit(
            self.row_count,
            self.identifier_width,
            self.policy_selectivity,
            self.query_selectivity,
            self.seed,
        )


def load_governed_pipeline_admission_config(
    path: Path | str,
) -> GovernedPipelineAdmissionConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return GovernedPipelineAdmissionConfig(
        results_dir=str(payload["results_dir"]),
        row_count=int(payload["row_count"]),
        identifier_widths=tuple(int(value) for value in payload["identifier_widths"]),
        policy_selectivities=tuple(float(value) for value in payload["policy_selectivities"]),
        query_selectivities=tuple(float(value) for value in payload["query_selectivities"]),
        join_match_rates=tuple(float(value) for value in payload["join_match_rates"]),
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
        minimum_distinct_singleton_winners=int(payload["minimum_distinct_singleton_winners"]),
        maximum_dominant_singleton_winner_fraction=float(
            payload["maximum_dominant_singleton_winner_fraction"]
        ),
        require_no_material_carryover=bool(payload["require_no_material_carryover"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def governed_pipeline_admission_units(
    config: GovernedPipelineAdmissionConfig,
) -> tuple[GovernedPipelineAdmissionUnit, ...]:
    return tuple(
        GovernedPipelineAdmissionUnit(
            config.row_count,
            width,
            policy,
            query,
            match,
            seed,
        )
        for width in config.identifier_widths
        for policy in config.policy_selectivities
        for query in config.query_selectivities
        for match in config.join_match_rates
        for seed in config.seeds
    )


def validate_declared_candidate_space(
    config: GovernedPipelineAdmissionConfig,
) -> None:
    """Reject a matrix whose declared factors mechanically collapse a route.

    This fast, data-free check runs before a result directory or any timing is
    created.  It uses the protocol's declared selectivities only as structural
    cardinalities; measured cardinalities are still checked again per seed.
    """

    for width in config.identifier_widths:
        for policy in config.policy_selectivities:
            for query in config.query_selectivities:
                for match in config.join_match_rates:
                    policy_rows = round(config.row_count * policy)
                    query_rows = round(config.row_count * query)
                    governed_rows = round(config.row_count * policy * query)
                    query_join_rows = round(config.row_count * query * match)
                    result_rows = round(config.row_count * policy * query * match)
                    statistics = GovernedPipelineStatistics(
                        input_rows=config.row_count,
                        estimated_policy_rows=policy_rows,
                        estimated_query_rows=query_rows,
                        estimated_governed_rows=governed_rows,
                        estimated_query_join_rows=query_join_rows,
                        estimated_result_rows=result_rows,
                        sensitive_width_bytes=float(width),
                    )
                    planning = plan_governed_pipeline(
                        statistics,
                        GovernanceFeasibilityPolicy(
                            "checkpoint-required",
                            None,
                            None,
                            require_governance_checkpoint=True,
                        ),
                    )
                    if planning.nondominated_candidate_ids != config.candidate_ids:
                        label = f"w{width}-p{policy}-q{query}-j{match}"
                        raise ValueError(
                            "Declared candidate space collapses before timing: "
                            f"{label}; survivors={planning.nondominated_candidate_ids}"
                        )


def _orders(
    candidate_ids: tuple[str, ...],
    repetitions: int,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    orders = list(itertools.permutations(candidate_ids)) * repetitions
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _create_data(connection: Any, unit: GovernedPipelineAdmissionUnit) -> dict[str, int]:
    """Create deterministic data with a controlled Join match rate."""

    for table in (
        "governed_output",
        "pipeline_checkpoint",
        "raw_join_checkpoint",
        "events",
        "dimension",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    domain = min(unit.row_count, 10_000)
    matching_keys = max(1, round(domain * unit.join_match_rate))
    blocks = math.ceil(unit.identifier_width / 32)
    connection.execute(
        f"""
        CREATE TABLE events AS
        SELECT i::BIGINT AS row_id,
               left(repeat(md5(CAST(i + {unit.seed * 1_000_003} AS VARCHAR)),
                           {blocks}), {unit.identifier_width}) AS sensitive_value,
               (i % {domain})::BIGINT AS join_key,
               (hash(i + {unit.seed * 97_003}) % 10000)::INTEGER AS query_bucket
        FROM range({unit.row_count}) AS source(i)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE dimension AS
        SELECT i::BIGINT AS dimension_key, (i % 97)::BIGINT AS marker
        FROM range({matching_keys}) AS source(i)
        """
    )
    policy_cutoff = unit.checkpoint_unit.policy_cutoff
    query_cutoff = unit.checkpoint_unit.query_cutoff
    row = connection.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE hash(sensitive_value) % 10000 < {policy_cutoff}),
          count(*) FILTER (WHERE query_bucket < {query_cutoff}),
          count(*) FILTER (WHERE hash(sensitive_value) % 10000 < {policy_cutoff}
                             AND query_bucket < {query_cutoff}),
          count(*) FILTER (WHERE query_bucket < {query_cutoff}
                             AND join_key < {matching_keys}),
          count(*) FILTER (WHERE hash(sensitive_value) % 10000 < {policy_cutoff}
                             AND query_bucket < {query_cutoff}
                             AND join_key < {matching_keys})
        FROM events
        """
    ).fetchone()
    if row is None:
        raise ValueError("Pipeline admission cardinality query returned no row")
    return {
        "policy_rows": int(row[0]),
        "query_rows": int(row[1]),
        "governed_rows": int(row[2]),
        "query_join_rows": int(row[3]),
        "result_rows": int(row[4]),
    }


def _drop_execution_tables(connection: Any) -> None:
    # Keep this superset synchronized with every executable candidate.  The
    # admission runner switches candidates inside one connection, so cleanup
    # cannot rely on the next candidate knowing its predecessor's tables.
    for table in (
        "governed_output",
        "pipeline_checkpoint",
        "raw_query_checkpoint",
        "raw_join_checkpoint",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _execute_timed(
    connection: Any,
    candidate: ExecutableGovernedPipeline,
    unit: GovernedPipelineAdmissionUnit,
    *,
    block_index: int,
    order_position: int,
    permutation_id: str,
) -> dict[str, object]:
    """Time DuckDB work; compute result and lineage digests afterwards."""

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
    result_digest = _digest(rows)
    lineage_digest = _digest(tuple((int(row[0]), int(row[1])) for row in rows))
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
        "client_materialization_latency_ms": latency_ms,
        "result_digest": result_digest,
        "lineage_digest": lineage_digest,
    }


def _digest(value: object) -> str:
    encoded = json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _run_unit(
    config: GovernedPipelineAdmissionConfig,
    unit: GovernedPipelineAdmissionUnit,
    progress: Callable[[int, int, str, float], None] | None,
    done_before: int,
    total_blocks: int,
    started: float,
) -> dict[str, object]:
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads TO {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        actual = _create_data(connection, unit)
        statistics = GovernedPipelineStatistics(
            input_rows=unit.row_count,
            estimated_policy_rows=actual["policy_rows"],
            estimated_query_rows=actual["query_rows"],
            estimated_governed_rows=actual["governed_rows"],
            estimated_query_join_rows=actual["query_join_rows"],
            estimated_result_rows=actual["result_rows"],
            sensitive_width_bytes=float(unit.identifier_width),
        )
        planning = plan_governed_pipeline(
            statistics,
            GovernanceFeasibilityPolicy(
                "checkpoint-required",
                None,
                None,
                require_governance_checkpoint=True,
            ),
        )
        if planning.nondominated_candidate_ids != config.candidate_ids:
            raise ValueError(f"Candidate space collapsed before timing: {unit.unit_id}")
        candidates = {
            candidate_id: build_executable_governed_pipeline(
                candidate_id,
                unit.checkpoint_unit,
            )
            for candidate_id in config.candidate_ids
        }
        plans = {
            candidate_id: observe_governed_pipeline_plan(connection, candidate)
            for candidate_id, candidate in candidates.items()
        }
        if len({item.combined_fingerprint for item in plans.values()}) != 3:
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
                raise ValueError(f"Warmup results differ: {unit.unit_id}")
            if len({item.lineage_digest for item in evidence}) != 1:
                raise ValueError(f"Warmup lineage differs: {unit.unit_id}")

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
            if len({row["result_digest"] for row in block}) != 1:
                raise ValueError(f"Measured results differ: {unit.unit_id}")
            if len({row["lineage_digest"] for row in block}) != 1:
                raise ValueError(f"Measured lineage differs: {unit.unit_id}")
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
        }
    finally:
        connection.close()


def _analyze(
    measurements: list[dict[str, str]],
    config: GovernedPipelineAdmissionConfig,
) -> dict[str, object]:
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in measurements:
        families[row["scenario_id"]].append(row)
    scenario_results: list[dict[str, object]] = []
    winners: list[str] = []
    for scenario_id, rows in sorted(families.items()):
        blocks: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in rows:
            blocks[(int(row["seed"]), int(row["block_index"]))][row["candidate_id"]] = float(
                row["latency_ms"]
            )
        dominated: set[str] = set()
        pairwise: list[dict[str, object]] = []
        for left, right in itertools.combinations(config.candidate_ids, 2):
            ratios: dict[int, list[float]] = defaultdict(list)
            for (seed, _block), values in sorted(blocks.items()):
                ratios[seed].append(math.log(values[left] / values[right]))
            stable = int.from_bytes(
                hashlib.sha256(
                    f"{config.bootstrap_seed}:{scenario_id}:{left}:{right}".encode()
                ).digest()[:8],
                "big",
            )
            point, lower, upper = hierarchical_paired_log_ratio_ci(
                ratios,
                confidence_level=config.confidence_level,
                repetitions=config.bootstrap_draws,
                seed=stable,
            )
            conclusion = classify_ratio_interval(
                lower,
                upper,
                config.practical_tie_fraction,
            )
            if conclusion == "LEFT_MATERIALLY_FASTER":
                dominated.add(right)
            elif conclusion == "LEFT_MATERIALLY_SLOWER":
                dominated.add(left)
            pairwise.append(
                {
                    "left": left,
                    "right": right,
                    "left_over_right_ratio": point,
                    "confidence_interval": [lower, upper],
                    "conclusion": conclusion,
                }
            )
        confidence_set = sorted(set(config.candidate_ids) - dominated)
        if len(confidence_set) == 1:
            winners.append(confidence_set[0])
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "confidence_undominated_candidate_ids": confidence_set,
                "pairwise": pairwise,
            }
        )

    counts = Counter(winners)
    conclusive_rate = len(winners) / len(scenario_results) if scenario_results else 0.0
    dominant_fraction = max(counts.values(), default=0) / max(len(winners), 1)

    # Block numbers restart for every seed.  Carryover must therefore be tested
    # inside each indivisible timing unit; pooling rows by block number would
    # silently mix unrelated workloads and invalidate the mirrored-order test.
    units: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in measurements:
        units[row["unit_id"]].append(row)
    carryover: list[dict[str, object]] = []
    for unit_id, rows in sorted(units.items()):
        unit_findings = assess_carryover(
            rows,
            candidate_ids=config.candidate_ids,
            carryover_candidate_ids=config.candidate_ids,
            tolerance_fraction=config.practical_tie_fraction,
            confidence_level=config.confidence_level,
            bootstrap_repetitions=config.bootstrap_draws,
            bootstrap_seed=config.bootstrap_seed,
            minimum_pairs=config.measured_rounds_per_permutation,
        )
        for finding in unit_findings:
            carryover.append({"unit_id": unit_id, **finding})
    # Local findings remain useful diagnostics, but gating on "any significant
    # result" across hundreds of tests creates a severe multiple-comparison
    # false-positive problem.  Gate the six polluter/target directions with
    # Bonferroni simultaneous confidence intervals over complete timing units.
    carryover_groups: dict[
        tuple[str, str],
        dict[int, list[float]],
    ] = defaultdict(dict)
    for index, finding in enumerate(carryover):
        ratio = finding.get("median_exposed_over_control_ratio")
        if not isinstance(ratio, (int, float)) or float(ratio) <= 0.0:
            continue
        numeric_ratio = float(ratio)
        pair = (
            str(finding["carryover_candidate_id"]),
            str(finding["target_candidate_id"]),
        )
        carryover_groups[pair][index] = [math.log(numeric_ratio)]
    simultaneous_confidence = 1.0 - (
        (1.0 - config.confidence_level) / max(len(carryover_groups), 1)
    )
    global_carryover: list[dict[str, object]] = []
    for (polluter, target), groups in sorted(carryover_groups.items()):
        stable = int.from_bytes(
            hashlib.sha256(
                f"{config.bootstrap_seed}:carryover:{polluter}:{target}".encode()
            ).digest()[:8],
            "big",
        )
        point, lower, upper = hierarchical_paired_log_ratio_ci(
            groups,
            confidence_level=simultaneous_confidence,
            repetitions=config.bootstrap_draws,
            seed=stable,
        )
        conclusion = classify_ratio_interval(
            lower,
            upper,
            config.practical_tie_fraction,
        )
        global_carryover.append(
            {
                "carryover_candidate_id": polluter,
                "target_candidate_id": target,
                "unit_count": len(groups),
                "median_exposed_over_control_ratio": point,
                "simultaneous_confidence_interval": [lower, upper],
                "classification": (
                    "NO_SYSTEMATIC_MATERIAL_CARRYOVER_AUTHORIZED"
                    if conclusion == "NO_PRACTICAL_DOMINANCE_AUTHORIZED"
                    else "SYSTEMATIC_MATERIAL_CARRYOVER_DETECTED"
                ),
            }
        )
    material_carryover = [
        item
        for item in global_carryover
        if item["classification"] == "SYSTEMATIC_MATERIAL_CARRYOVER_DETECTED"
    ]
    gates = {
        "minimum_conclusive_scenario_rate": (
            conclusive_rate >= config.minimum_conclusive_scenario_rate
        ),
        "minimum_distinct_singleton_winners": (
            len(counts) >= config.minimum_distinct_singleton_winners
        ),
        "maximum_dominant_singleton_winner_fraction": (
            dominant_fraction <= config.maximum_dominant_singleton_winner_fraction
        ),
        "no_material_carryover": (
            not material_carryover or not config.require_no_material_carryover
        ),
    }
    passed = all(gates.values())
    return {
        "status": (
            "PASS_GOVERNED_PIPELINE_OPTIMIZER_ADMISSION"
            if passed
            else "FAIL_GOVERNED_PIPELINE_OPTIMIZER_ADMISSION_RETAIN"
        ),
        "optimizer_training_authorized": passed,
        "scenario_count": len(scenario_results),
        "conclusive_scenario_rate": conclusive_rate,
        "singleton_winner_counts": dict(sorted(counts.items())),
        "dominant_singleton_winner_fraction": dominant_fraction,
        "carryover_findings": carryover,
        "carryover_global_findings": global_carryover,
        "carryover_simultaneous_confidence_level": simultaneous_confidence,
        "carryover_multiple_testing_correction": "Bonferroni",
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "scenario_results": scenario_results,
        "paper_optimizer_performance_claim_authorized": False,
    }


def run_governed_pipeline_admission(
    config: GovernedPipelineAdmissionConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run or resume the frozen admission matrix one complete unit at a time."""

    root = project_root.resolve()
    config_path = config_path.resolve()
    validate_declared_candidate_space(config)
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Pipeline admission requires a clean worktree")
    results_root = root / config.results_dir
    if resume_run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = results_root / run_id
        output.mkdir(parents=True, exist_ok=False)
        (output / "units").mkdir()
        frozen_config = json.loads(json.dumps(asdict(config)))
        _atomic_json(output / "config.json", frozen_config)
        environment = _environment(commit, dirty, cast(Any, config))
        environment.update(
            {
                "config_path": str(config_path),
                "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            }
        )
        _atomic_json(output / "environment.json", environment)
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    else:
        output = results_root / resume_run_id
        frozen = json.loads((output / "config.json").read_text(encoding="utf-8"))
        current = json.loads(json.dumps(asdict(config)))
        if frozen != current:
            raise ValueError("Resume config differs from the frozen run")

    units = governed_pipeline_admission_units(config)
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
            progress,
            completed,
            total_blocks,
            started,
        )
        _atomic_json(unit_path, payload)
        completed += config.measured_blocks_per_unit

    rows: list[dict[str, object]] = []
    for unit in units:
        payload = json.loads(
            (output / "units" / f"{unit.unit_id}.json").read_text(encoding="utf-8")
        )
        unit_fields = cast(dict[str, object], payload["unit"])
        actual = cast(dict[str, object], payload["actual_cardinalities"])
        for measurement in cast(list[dict[str, object]], payload["measurements"]):
            rows.append({**unit_fields, **actual, **measurement})
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    string_rows = [{key: str(value) for key, value in row.items()} for row in rows]
    _atomic_json(output / "summary.json", _analyze(string_rows, config))
    return output
