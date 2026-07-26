"""Conservative claim authorization for randomized paired timing blocks.

The helpers in this module deliberately separate three questions:

1. Did one candidate change the latency of a candidate that followed it?
2. Which blocks are safe from that predeclared carryover candidate?
3. Does a paired confidence interval support one specific performance claim?

This prevents an unstable materialization route from silently warming or
throttling a later route, and prevents a point estimate from authorizing a
paper claim on its own.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _percentile(values: Sequence[float], probability: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""

    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _derived_seed(seed: int, label: str) -> int:
    """Derive a stable per-claim seed without Python's randomized hash()."""

    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def stratified_paired_bootstrap_ci(
    ratios_by_stratum: Mapping[str, Sequence[float]],
    *,
    confidence_level: float,
    repetitions: int,
    seed: int,
) -> tuple[float, float]:
    """Bootstrap the median paired ratio while preserving order strata.

    Each input ratio compares two candidates inside the same timing block.
    Resampling happens independently inside each execution-order stratum so a
    bootstrap draw cannot accidentally over-represent one candidate order.
    """

    strata = {key: tuple(values) for key, values in ratios_by_stratum.items() if values}
    if not strata:
        raise ValueError("paired bootstrap requires at least one ratio")
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be in (0, 1)")
    if repetitions < 1000:
        raise ValueError("paired bootstrap requires at least 1000 repetitions")
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(repetitions):
        sample: list[float] = []
        for values in strata.values():
            sample.extend(values[rng.randrange(len(values))] for _ in values)
        estimates.append(statistics.median(sample))
    alpha = (1.0 - confidence_level) / 2.0
    return _percentile(estimates, alpha), _percentile(estimates, 1.0 - alpha)


def _block_medians(rows: Iterable[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    """Collapse repeated executions to the predeclared block-level unit."""

    values: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    orders: dict[int, tuple[str, ...]] = {}
    for row in rows:
        block = int(row["block_index"])
        candidate = str(row["candidate_id"])
        values[block][candidate].append(float(row["client_materialization_latency_ms"]))
        orders[block] = tuple(part.strip() for part in str(row["permutation_id"]).split("->"))
    return {
        block: {
            "order": orders[block],
            "latencies": {
                candidate: statistics.median(candidate_values)
                for candidate, candidate_values in candidates.items()
            },
        }
        for block, candidates in values.items()
    }


def _paired_ci(
    ratios_by_stratum: Mapping[str, Sequence[float]],
    *,
    confidence_level: float,
    repetitions: int,
    seed: int,
    label: str,
) -> tuple[float, float]:
    return stratified_paired_bootstrap_ci(
        ratios_by_stratum,
        confidence_level=confidence_level,
        repetitions=repetitions,
        seed=_derived_seed(seed, label),
    )


def assess_carryover(
    rows: Iterable[Mapping[str, Any]],
    *,
    candidate_ids: Sequence[str],
    carryover_candidate_ids: Sequence[str],
    tolerance_fraction: float,
    confidence_level: float,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
    minimum_pairs: int,
) -> list[dict[str, Any]]:
    """Test mirrored middle-position orders for first-order carryover.

    For candidates P (possible polluter), T (target), and N (neutral), the
    balanced schedule contains both ``P -> T -> N`` and ``N -> T -> P``.
    T occupies the same middle position in both orders. Pairing the kth
    occurrence of those mirrored orders therefore isolates whether executing
    P immediately before T materially changes T's latency.
    """

    blocks = _block_medians(rows)
    by_order: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for block, payload in blocks.items():
        by_order[payload["order"]].append((block, payload))
    for values in by_order.values():
        values.sort(key=lambda item: item[0])

    findings: list[dict[str, Any]] = []
    lower: float | None
    upper: float | None
    for polluter in carryover_candidate_ids:
        for target in candidate_ids:
            if target == polluter:
                continue
            neutral = next(item for item in candidate_ids if item not in {polluter, target})
            exposed = by_order.get((polluter, target, neutral), [])
            control = by_order.get((neutral, target, polluter), [])
            pair_count = min(len(exposed), len(control))
            ratios = [
                exposed[index][1]["latencies"][target] / control[index][1]["latencies"][target]
                for index in range(pair_count)
            ]
            if pair_count >= minimum_pairs:
                lower, upper = _paired_ci(
                    {"mirrored_middle_position": ratios},
                    confidence_level=confidence_level,
                    repetitions=bootstrap_repetitions,
                    seed=bootstrap_seed,
                    label=f"carryover:{polluter}:{target}",
                )
                if lower >= 1.0 - tolerance_fraction and upper <= 1.0 + tolerance_fraction:
                    classification = "NO_MATERIAL_CARRYOVER"
                elif upper < 1.0 - tolerance_fraction or lower > 1.0 + tolerance_fraction:
                    classification = "MATERIAL_CARRYOVER_DETECTED"
                else:
                    classification = "INCONCLUSIVE"
            else:
                lower = upper = None
                classification = "INSUFFICIENT_PAIRS"
            findings.append(
                {
                    "carryover_candidate_id": polluter,
                    "target_candidate_id": target,
                    "pair_count": pair_count,
                    "median_exposed_over_control_ratio": (
                        statistics.median(ratios) if ratios else None
                    ),
                    "confidence_interval": {
                        "level": confidence_level,
                        "lower": lower,
                        "upper": upper,
                    },
                    "tolerance_fraction": tolerance_fraction,
                    "classification": classification,
                }
            )
    return findings


def _pollution_safe(
    order: Sequence[str],
    *,
    baseline_id: str,
    candidate_id: str,
    carryover_candidate_ids: Sequence[str],
) -> bool:
    """Require every possible polluter to execute after the compared route(s)."""

    positions = {candidate: order.index(candidate) for candidate in order}
    for polluter in carryover_candidate_ids:
        if polluter == candidate_id:
            if positions[polluter] <= positions[baseline_id]:
                return False
        elif polluter == baseline_id:
            if positions[polluter] <= positions[candidate_id]:
                return False
        elif positions[polluter] <= max(positions[baseline_id], positions[candidate_id]):
            return False
    return True


def authorize_paired_claims(
    rows: Iterable[Mapping[str, Any]],
    *,
    candidate_ids: Sequence[str],
    baseline_id: str,
    carryover_candidate_ids: Sequence[str],
    tie_fraction: float,
    confidence_level: float,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
    minimum_blocks: int,
) -> list[dict[str, Any]]:
    """Authorize baseline comparisons only from pollution-safe paired blocks."""

    blocks = _block_medians(rows)
    claims: list[dict[str, Any]] = []
    lower: float | None
    upper: float | None
    for candidate in candidate_ids:
        if candidate == baseline_id:
            continue
        ratios_by_order: dict[str, list[float]] = defaultdict(list)
        block_ids: list[int] = []
        for block, payload in sorted(blocks.items()):
            order = payload["order"]
            if not _pollution_safe(
                order,
                baseline_id=baseline_id,
                candidate_id=candidate,
                carryover_candidate_ids=carryover_candidate_ids,
            ):
                continue
            ratio = payload["latencies"][candidate] / payload["latencies"][baseline_id]
            ratios_by_order[" -> ".join(order)].append(ratio)
            block_ids.append(block)
        ratios = [value for values in ratios_by_order.values() for value in values]
        if len(ratios) >= minimum_blocks:
            lower, upper = _paired_ci(
                ratios_by_order,
                confidence_level=confidence_level,
                repetitions=bootstrap_repetitions,
                seed=bootstrap_seed,
                label=f"claim:{candidate}:{baseline_id}",
            )
            if upper < 1.0 - tie_fraction:
                conclusion = "MATERIALLY_FASTER"
            elif lower > 1.0 + tie_fraction:
                conclusion = "MATERIALLY_SLOWER"
            elif lower >= 1.0 - tie_fraction and upper <= 1.0 + tie_fraction:
                conclusion = "PRACTICALLY_EQUIVALENT"
            else:
                conclusion = "INCONCLUSIVE"
        else:
            lower = upper = None
            conclusion = "INSUFFICIENT_BLOCKS"
        authorized = conclusion in {
            "MATERIALLY_FASTER",
            "MATERIALLY_SLOWER",
            "PRACTICALLY_EQUIVALENT",
        }
        claims.append(
            {
                "candidate_id": candidate,
                "baseline_id": baseline_id,
                "pollution_safe_block_count": len(ratios),
                "pollution_safe_block_ids": block_ids,
                "median_candidate_over_baseline_ratio": (
                    statistics.median(ratios) if ratios else None
                ),
                "confidence_interval": {
                    "method": "permutation_stratified_paired_bootstrap_median_ratio_v1",
                    "level": confidence_level,
                    "lower": lower,
                    "upper": upper,
                    "repetitions": bootstrap_repetitions,
                    "seed": bootstrap_seed,
                },
                "tie_fraction": tie_fraction,
                "conclusion": conclusion,
                "claim_authorized": authorized,
            }
        )
    return claims
