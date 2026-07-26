"""Paired winner-diversity admission for record-lineage checkpoint reuse."""

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
from typing import Any

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
from trustaero.experiments.lineage_checkpoint_mechanism import (
    LineageBatchQuery,
    execute_lineage_checkpoint_batch,
    observe_lineage_checkpoint_plan,
)
from trustaero.optimizer.lineage_checkpoint_space import (
    LINEAGE_CHECKPOINT_CANDIDATE_IDS,
)


@dataclass(frozen=True, slots=True)
class LineageCheckpointScenario:
    """One batch shape kept intact across three independent data seeds."""

    scenario_id: str
    query_count: int
    policy_selectivities: tuple[float, ...]
    query_selectivities: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.scenario_id or self.query_count <= 0:
            raise ValueError("Lineage scenario ID and query count are required")
        if not self.policy_selectivities or not self.query_selectivities:
            raise ValueError("Lineage scenario selectivities cannot be empty")
        values = self.policy_selectivities + self.query_selectivities
        if any(not 0.0 < value <= 1.0 for value in values):
            raise ValueError("Lineage scenario selectivities must be in (0, 1]")

    def queries(self) -> tuple[LineageBatchQuery, ...]:
        """Generate a deterministic heterogeneous query batch."""

        return tuple(
            LineageBatchQuery(
                query_id=f"{self.scenario_id}-q{index:02d}",
                policy_cutoff=round(
                    self.policy_selectivities[index % len(self.policy_selectivities)] * 10_000
                ),
                query_cutoff=round(
                    self.query_selectivities[index % len(self.query_selectivities)] * 10_000
                ),
            )
            for index in range(self.query_count)
        )


@dataclass(frozen=True, slots=True)
class LineageCheckpointAdmissionConfig:
    """Frozen data, workload, timing, and confidence gates."""

    results_dir: str
    row_count: int
    seeds: tuple[int, ...]
    scenarios: tuple[LineageCheckpointScenario, ...]
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
    maximum_dominant_winner_fraction: float
    require_clean_git: bool

    def __post_init__(self) -> None:
        if self.row_count <= 0 or len(self.seeds) < 3:
            raise ValueError("Lineage admission requires rows and three seeds")
        if self.candidate_ids != LINEAGE_CHECKPOINT_CANDIDATE_IDS:
            raise ValueError("Lineage checkpoint candidate set changed")
        if not self.scenarios or len({item.scenario_id for item in self.scenarios}) != len(
            self.scenarios
        ):
            raise ValueError("Lineage admission scenarios must be nonempty and unique")
        if self.warmup_rounds_per_permutation < 1:
            raise ValueError("Lineage admission requires balanced warmup")
        if self.measured_rounds_per_permutation < 5:
            raise ValueError("Lineage admission requires five measured rounds")
        if self.bootstrap_draws < 1_000:
            raise ValueError("Lineage admission requires at least 1000 bootstrap draws")
        if not 2 <= self.minimum_distinct_singleton_winners <= len(self.candidate_ids):
            raise ValueError("Lineage admission requires at least two winners")

    @property
    def blocks_per_unit(self) -> int:
        return math.factorial(len(self.candidate_ids)) * self.measured_rounds_per_permutation


def load_lineage_checkpoint_admission_config(
    path: Path | str,
) -> LineageCheckpointAdmissionConfig:
    """Load a frozen JSON admission protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = tuple(
        LineageCheckpointScenario(
            scenario_id=str(item["scenario_id"]),
            query_count=int(item["query_count"]),
            policy_selectivities=tuple(float(value) for value in item["policy_selectivities"]),
            query_selectivities=tuple(float(value) for value in item["query_selectivities"]),
        )
        for item in payload["scenarios"]
    )
    return LineageCheckpointAdmissionConfig(
        results_dir=str(payload["results_dir"]),
        row_count=int(payload["row_count"]),
        seeds=tuple(int(value) for value in payload["seeds"]),
        scenarios=scenarios,
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
        maximum_dominant_winner_fraction=float(payload["maximum_dominant_winner_fraction"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def _orders(
    candidate_ids: tuple[str, ...],
    repetitions: int,
    seed: int,
) -> tuple[tuple[str, ...], ...]:
    orders = list(itertools.permutations(candidate_ids)) * repetitions
    random.Random(seed).shuffle(orders)
    return tuple(orders)


def _create_data(connection: Any, row_count: int, seed: int) -> None:
    connection.execute("DROP TABLE IF EXISTS lineage_events")
    connection.execute(
        f"""
        CREATE TABLE lineage_events AS
        SELECT 'event-' || CAST(i + {seed * 1_000_003} AS VARCHAR) AS event_id,
               (hash(i * 17 + {seed * 97_003}) % 10000)::INTEGER AS policy_bucket,
               (hash(i * 29 + {seed * 193_009}) % 10000)::INTEGER AS query_bucket,
               (i % 101)::BIGINT AS event_value
        FROM range({row_count}) AS source(i)
        """
    )


def _evidence_key(execution: Any) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.query_id,
            item.row_count,
            item.result_digest,
            item.edge_digest,
            item.evidence_bytes,
        )
        for item in execution.query_evidence
    )


def _analyze(
    measurements: list[dict[str, object]],
    config: LineageCheckpointAdmissionConfig,
) -> dict[str, object]:
    families: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in measurements:
        families[str(row["scenario_id"])].append(row)
    scenario_results: list[dict[str, object]] = []
    winners: list[str] = []
    for scenario_id, rows in sorted(families.items()):
        blocks: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in rows:
            blocks[(int(str(row["seed"])), int(str(row["block_index"])))][
                str(row["candidate_id"])
            ] = float(str(row["latency_ms"]))
        dominated: set[str] = set()
        pairwise: list[dict[str, object]] = []
        for left, right in itertools.combinations(config.candidate_ids, 2):
            ratios: dict[int, list[float]] = defaultdict(list)
            for (seed, _), values in sorted(blocks.items()):
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
            conclusion = classify_ratio_interval(lower, upper, config.practical_tie_fraction)
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
    conclusive_rate = len(winners) / max(len(scenario_results), 1)
    dominant_fraction = max(counts.values(), default=0) / max(len(winners), 1)
    gates = {
        "minimum_conclusive_scenario_rate": (
            conclusive_rate >= config.minimum_conclusive_scenario_rate
        ),
        "minimum_distinct_singleton_winners": (
            len(counts) >= config.minimum_distinct_singleton_winners
        ),
        "maximum_dominant_winner_fraction": (
            dominant_fraction <= config.maximum_dominant_winner_fraction
        ),
    }
    passed = all(gates.values())
    return {
        "status": (
            "PASS_LINEAGE_CHECKPOINT_OPTIMIZER_ADMISSION"
            if passed
            else "FAIL_LINEAGE_CHECKPOINT_OPTIMIZER_ADMISSION_RETAIN"
        ),
        "singleton_winner_counts": dict(sorted(counts.items())),
        "conclusive_scenario_rate": conclusive_rate,
        "dominant_winner_fraction": dominant_fraction,
        "gates": gates,
        "scenario_results": scenario_results,
    }


def run_lineage_checkpoint_admission(
    config: LineageCheckpointAdmissionConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    progress: Callable[[int, int, str, float], None] | None = None,
) -> Path:
    """Run the frozen workload with balanced candidate permutations."""

    import duckdb

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Frozen lineage admission requires a clean Git worktree")
    run_id = resume_run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    manifest_path = output_dir / "run_manifest.json"
    if resume_run_id is None:
        output_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(
            manifest_path,
            {
                "run_id": run_id,
                "commit_hash": commit,
                "config_sha256": config_sha256,
            },
        )
    else:
        if not manifest_path.is_file():
            raise ValueError(f"Lineage admission resume manifest is missing: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("commit_hash") != commit or manifest.get("config_sha256") != config_sha256:
            raise ValueError("Lineage admission resume binding changed")
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_id, "status": "RUNNING"},
    )
    unit_dir = output_dir / "units"
    unit_dir.mkdir(exist_ok=True)
    total_blocks = len(config.scenarios) * len(config.seeds) * config.blocks_per_unit
    measurements: list[dict[str, object]] = []
    units: list[dict[str, object]] = []
    done = 0
    started = time.perf_counter()
    for scenario in config.scenarios:
        queries = scenario.queries()
        for seed in config.seeds:
            unit_path = unit_dir / f"{scenario.scenario_id}-s{seed}.json"
            if unit_path.is_file():
                saved = json.loads(unit_path.read_text(encoding="utf-8"))
                measurements.extend(saved["measurements"])
                units.append(saved["unit"])
                done += config.blocks_per_unit
                if progress is not None:
                    progress(
                        done,
                        total_blocks,
                        f"{scenario.scenario_id}-s{seed} reused",
                        time.perf_counter() - started,
                    )
                continue
            connection = duckdb.connect(":memory:")
            try:
                connection.execute(f"SET threads TO {config.duckdb_threads}")
                connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
                _create_data(connection, config.row_count, seed)
                plans = {
                    candidate_id: observe_lineage_checkpoint_plan(connection, candidate_id, queries)
                    for candidate_id in config.candidate_ids
                }
                if len(set(plans.values())) != len(config.candidate_ids):
                    raise ValueError(
                        f"Lineage physical plans collapsed: {scenario.scenario_id}-s{seed}"
                    )
                stable = int.from_bytes(
                    hashlib.sha256(f"{scenario.scenario_id}:{seed}".encode()).digest()[:4],
                    "big",
                )
                for order in _orders(
                    config.candidate_ids,
                    config.warmup_rounds_per_permutation,
                    config.order_seed + stable,
                ):
                    warmups = [
                        execute_lineage_checkpoint_batch(connection, candidate_id, queries)
                        for candidate_id in order
                    ]
                    if len({_evidence_key(item) for item in warmups}) != 1:
                        raise ValueError(
                            f"Lineage warmup evidence differs: {scenario.scenario_id}-s{seed}"
                        )
                for block_index, order in enumerate(
                    _orders(
                        config.candidate_ids,
                        config.measured_rounds_per_permutation,
                        config.order_seed + stable + 1,
                    )
                ):
                    executions = [
                        execute_lineage_checkpoint_batch(connection, candidate_id, queries)
                        for candidate_id in order
                    ]
                    if len({_evidence_key(item) for item in executions}) != 1:
                        raise ValueError(
                            f"Lineage measured evidence differs: {scenario.scenario_id}-s{seed}"
                        )
                    for position, execution in enumerate(executions):
                        measurements.append(
                            {
                                "scenario_id": scenario.scenario_id,
                                "unit_id": f"{scenario.scenario_id}-s{seed}",
                                "seed": seed,
                                "candidate_id": execution.candidate_id,
                                "block_index": block_index,
                                "order_position": position,
                                "permutation_id": "->".join(order),
                                "latency_ms": execution.latency_ms,
                                "checkpoint_rows": execution.checkpoint_rows,
                                "query_count": scenario.query_count,
                                "distinct_policy_count": len(scenario.policy_selectivities),
                            }
                        )
                    done += 1
                    if progress is not None:
                        progress(
                            done,
                            total_blocks,
                            f"{scenario.scenario_id}-s{seed} block={block_index + 1}",
                            time.perf_counter() - started,
                        )
                unit_record = {
                    "scenario": asdict(scenario),
                    "seed": seed,
                    "plan_fingerprints": plans,
                    "evidence_digest": _evidence_key(executions[0]),
                }
                units.append(unit_record)
                unit_measurements = [
                    row
                    for row in measurements
                    if row["unit_id"] == f"{scenario.scenario_id}-s{seed}"
                ]
                _atomic_json(
                    unit_path,
                    {
                        "unit": unit_record,
                        "measurements": unit_measurements,
                    },
                )
            finally:
                connection.close()
    with (output_dir / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(measurements[0]))
        writer.writeheader()
        writer.writerows(measurements)
    analysis = _analyze(measurements, config)
    summary = {
        **analysis,
        "run_id": run_id,
        "commit_hash": commit,
        "git_dirty": dirty,
        "query_family": "record_lineage_checkpoint_reuse_v1",
        "config_path": config_path.resolve().relative_to(root).as_posix(),
        "paired_block_count": total_blocks,
        "candidate_execution_count": len(measurements),
        "units": units,
        # The shared environment helper only reads thread/memory fields, which
        # this frozen config intentionally provides with identical semantics.
        "environment": _environment(commit, dirty, config),  # type: ignore[arg-type]
    }
    _atomic_json(output_dir / "summary.json", summary)
    _atomic_json(
        root / config.results_dir / "latest_run.json",
        {"run_id": run_id, "status": analysis["status"]},
    )
    return output_dir
