"""Paired, set-valued inference for a completed EA-0 mechanism audit.

EA-0 deliberately executes every legal variant in the same measured round.
This module therefore compares candidates *within* a round before combining
random seeds.  That pairing removes much of the machine-wide slowdown shared
by all candidates in a round.  It does not pretend that background activity is
harmless: absolute dispersion and early/late drift are retained as diagnostics.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    execution_flow_variants,
)


def _percentile(values: Sequence[float], probability: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sequence."""

    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _stable_seed(seed: int, label: str) -> int:
    """Give every pair an order-independent deterministic bootstrap stream."""

    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def hierarchical_paired_log_ratio_ci(
    log_ratios_by_seed: Mapping[int, Sequence[float]],
    *,
    confidence_level: float,
    repetitions: int,
    seed: int,
) -> tuple[float, float, float]:
    """Estimate a median latency ratio with seed-then-round resampling.

    A bootstrap draw first samples complete data seeds with replacement and
    then samples paired rounds within each selected seed.  This preserves the
    experimental unit and avoids treating 33 correlated timings as 33 fully
    independent data sets.  Calculations happen in log space; returned values
    are ordinary candidate/baseline latency ratios.
    """

    groups = {
        int(group_seed): tuple(float(value) for value in values)
        for group_seed, values in log_ratios_by_seed.items()
        if values
    }
    if not groups:
        raise ValueError("hierarchical bootstrap requires at least one seed")
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be in (0, 1)")
    if repetitions < 1000:
        raise ValueError("hierarchical bootstrap requires at least 1000 draws")
    seed_ids = tuple(sorted(groups))
    observed = math.exp(statistics.median(value for values in groups.values() for value in values))
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(repetitions):
        sample: list[float] = []
        for _seed_slot in seed_ids:
            selected_seed = seed_ids[rng.randrange(len(seed_ids))]
            rounds = groups[selected_seed]
            sample.extend(rounds[rng.randrange(len(rounds))] for _ in rounds)
        estimates.append(math.exp(statistics.median(sample)))
    alpha = (1.0 - confidence_level) / 2.0
    return (
        observed,
        _percentile(estimates, alpha),
        _percentile(estimates, 1.0 - alpha),
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return cast(dict[str, Any], payload)


def _load_measurements(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("EA-0 measurements.csv is empty")
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pair_drift(log_ratios_by_seed: Mapping[int, Sequence[float]]) -> float:
    """Report, but do not post-hoc gate, first-half versus second-half drift."""

    first: list[float] = []
    second: list[float] = []
    for values in log_ratios_by_seed.values():
        midpoint = len(values) // 2
        first.extend(values[:midpoint])
        second.extend(values[-midpoint:])
    if not first or not second:
        return 0.0
    return abs(math.exp(statistics.median(second) - statistics.median(first)) - 1.0)


def analyze_execution_flow_inference(
    run_dir: Path,
    protocol_path: Path,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Apply the frozen EA-0 paired rule without training or selecting a model."""

    run_dir = run_dir.resolve()
    protocol_path = protocol_path.resolve()
    protocol = _load_json(protocol_path)
    summary = _load_json(run_dir / "summary.json")
    environment = _load_json(run_dir / "environment.json")
    config = _load_json(run_dir / "config.json")
    measurements_path = run_dir / "measurements.csv"
    rows = _load_measurements(measurements_path)
    timing = cast(dict[str, Any], protocol["timing_protocol"])
    matrix = cast(dict[str, Any], protocol["formal_matrix"])
    tie = float(timing["practical_tie_fraction"])
    confidence = 0.95
    repetitions = int(timing["bootstrap_draws"])
    bootstrap_seed = int(timing["bootstrap_seed"])

    # Fail closed if the completed artifact is not the exact frozen matrix.
    expected_count = int(matrix["measured_execution_count"])
    integrity = {
        "runner_status_passed": summary.get("status") == "PASS_EXECUTION_FLOW_AUDIT",
        "clean_source_commit": environment.get("git_dirty") is False,
        "measurement_count_matches_protocol": len(rows) == expected_count,
        "summary_measurement_count_matches": int(summary["measurement_count"]) == len(rows),
        "unit_count_matches_protocol": int(summary["unit_count"]) == int(matrix["unit_count"]),
        "optimizer_not_trained": summary.get("optimizer_trained") is False,
        "exact_operator_bytes_not_claimed": (
            summary.get("engine_reported_per_operator_payload_bytes") is False
        ),
    }
    if not all(integrity.values()):
        raise ValueError(f"EA-0 inference integrity check failed: {integrity}")

    variants = {item.variant_id: item for item in execution_flow_variants()}
    groups: dict[tuple[int, int, float, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        variant = variants[row["variant_id"]]
        if variant.equivalence_group != row["equivalence_group"]:
            raise ValueError("Measurement equivalence-group metadata changed")
        key = (
            int(row["row_count"]),
            int(row["identifier_width"]),
            float(row["match_rate"]),
            row["equivalence_group"],
        )
        groups[key].append(row)

    pair_jobs: list[tuple[tuple[int, int, float, str], str, str]] = []
    for key, group_rows in sorted(groups.items()):
        candidate_ids = sorted({row["variant_id"] for row in group_rows})
        for left_index, left in enumerate(candidate_ids):
            for right in candidate_ids[left_index + 1 :]:
                pair_jobs.append((key, left, right))

    pair_results: list[dict[str, Any]] = []
    for job_index, (key, left, right) in enumerate(pair_jobs, start=1):
        group_rows = groups[key]
        by_block: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for row in group_rows:
            by_block[(int(row["seed"]), int(row["repeat_index"]))][row["variant_id"]] = float(
                row["latency_ms"]
            )
        ratios_by_seed: dict[int, list[float]] = defaultdict(list)
        for (data_seed, repeat_index), latencies in sorted(by_block.items()):
            if left not in latencies or right not in latencies:
                raise ValueError(f"Incomplete paired round: {key}/{data_seed}/{repeat_index}")
            ratios_by_seed[data_seed].append(math.log(latencies[left] / latencies[right]))
        expected_seeds = len(cast(list[Any], matrix["seeds"]))
        measured_runs = int(matrix["measured_runs_per_variant"])
        if len(ratios_by_seed) != expected_seeds or any(
            len(values) != measured_runs for values in ratios_by_seed.values()
        ):
            raise ValueError(f"Paired seed/round matrix is incomplete: {key}")
        label = f"{key}:{left}:{right}"
        ratio, lower, upper = hierarchical_paired_log_ratio_ci(
            ratios_by_seed,
            confidence_level=confidence,
            repetitions=repetitions,
            seed=_stable_seed(bootstrap_seed, label),
        )
        if lower > 1.0 + tie:
            conclusion = "LEFT_MATERIALLY_SLOWER"
        elif upper < 1.0 / (1.0 + tie):
            conclusion = "LEFT_MATERIALLY_FASTER"
        else:
            conclusion = "NO_PRACTICAL_DOMINANCE_AUTHORIZED"
        pair_results.append(
            {
                "row_count": key[0],
                "identifier_width": key[1],
                "match_rate": key[2],
                "equivalence_group": key[3],
                "left_variant_id": left,
                "right_variant_id": right,
                "paired_round_count": sum(len(values) for values in ratios_by_seed.values()),
                "data_seed_count": len(ratios_by_seed),
                "median_left_over_right_ratio": ratio,
                "confidence_interval_95": [lower, upper],
                "practical_tie_fraction": tie,
                "conclusion": conclusion,
                "first_second_half_ratio_drift": _pair_drift(ratios_by_seed),
            }
        )
        if progress_callback is not None:
            progress_callback(job_index, len(pair_jobs), label)

    # A candidate is removed only by a confidence-authorized >3% comparison.
    family_results: list[dict[str, Any]] = []
    for key, group_rows in sorted(groups.items()):
        candidates = sorted({row["variant_id"] for row in group_rows})
        dominated: set[str] = set()
        relevant = [
            item
            for item in pair_results
            if (
                item["row_count"],
                item["identifier_width"],
                item["match_rate"],
                item["equivalence_group"],
            )
            == key
        ]
        for item in relevant:
            if item["conclusion"] == "LEFT_MATERIALLY_SLOWER":
                dominated.add(str(item["left_variant_id"]))
            elif item["conclusion"] == "LEFT_MATERIALLY_FASTER":
                dominated.add(str(item["right_variant_id"]))
        family_results.append(
            {
                "row_count": key[0],
                "identifier_width": key[1],
                "match_rate": key[2],
                "equivalence_group": key[3],
                "candidate_ids": candidates,
                "dominated_candidate_ids": sorted(dominated),
                "non_dominated_candidate_ids": sorted(set(candidates) - dominated),
            }
        )

    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS_EA0_PAIRED_INFERENCE",
        "source_run_dir": run_dir.as_posix(),
        "source_commit_hash": environment["commit_hash"],
        "source_measurements_sha256": _sha256(measurements_path),
        "protocol_path": protocol_path.as_posix(),
        "protocol_sha256": _sha256(protocol_path),
        "integrity_checks": integrity,
        "inference_method": {
            "estimand": "median paired within-round log latency ratio",
            "resampling": "complete data seeds, then paired rounds within seed",
            "confidence_level": confidence,
            "bootstrap_draws": repetitions,
            "bootstrap_seed": bootstrap_seed,
            "practical_tie_fraction": tie,
        },
        "family_count": len(family_results),
        "pairwise_comparison_count": len(pair_results),
        "family_results": family_results,
        "pairwise_results": pair_results,
        "optimizer_trained": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This post-run analysis applies the inference method frozen before EA-0. "
            "It authorizes only candidate mechanism contrasts and set-valued physical-"
            "work evidence; it does not evaluate an optimizer or report exact DuckDB "
            "per-operator payload bytes. Background workload is handled conservatively "
            "through paired inference and remains visible in drift diagnostics."
        ),
        "source_config": config,
    }
    _atomic_json(run_dir / "paired_inference.json", result)
    return result
