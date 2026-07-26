"""Confidence-aware audit of development Oracle labels.

The original evaluator chose an Oracle set independently for each data seed
from median latency. A noisy seed can therefore manufacture a large regret.
This module does not retrain the optimizer. It combines paired rounds across
complete seeds and removes a candidate only when a confidence interval proves
a practically meaningful (>3%) disadvantage.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from trustaero.experiments.execution_aware_candidate_calibration import (
    DEPLOYABLE_STABLE_PREFERENCES,
)
from trustaero.experiments.execution_flow_audit import (
    _atomic_json,
    _git_state,
    execution_flow_variants,
)
from trustaero.experiments.execution_flow_inference import (
    hierarchical_paired_log_ratio_ci,
)

PairConclusion = Literal[
    "LEFT_MATERIALLY_FASTER",
    "LEFT_MATERIALLY_SLOWER",
    "NO_PRACTICAL_DOMINANCE_AUTHORIZED",
]


@dataclass(frozen=True, slots=True)
class OracleStabilityConfig:
    """Frozen inputs and inference controls for the development audit."""

    source_run_dir: str
    candidate_result_path: str
    tail_confirmation_path: str
    results_dir: str
    deployable_equivalence_groups: tuple[str, ...]
    practical_tie_fraction: float
    confidence_level: float
    bootstrap_draws: int
    bootstrap_seed: int
    expected_measurements_sha256: str
    expected_candidate_result_sha256: str
    expected_tail_confirmation_sha256: str
    require_clean_git: bool

    def __post_init__(self) -> None:
        expected_groups = tuple(sorted(DEPLOYABLE_STABLE_PREFERENCES))
        if tuple(sorted(self.deployable_equivalence_groups)) != expected_groups:
            raise ValueError("Oracle audit must retain every deployable group")
        if not 0.0 < self.practical_tie_fraction < 0.25:
            raise ValueError("Oracle-audit practical tie is invalid")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("Oracle-audit confidence level is invalid")
        if self.bootstrap_draws < 1000:
            raise ValueError("Oracle audit requires at least 1000 bootstrap draws")
        hashes = (
            self.expected_measurements_sha256,
            self.expected_candidate_result_sha256,
            self.expected_tail_confirmation_sha256,
        )
        if any(len(value) != 64 for value in hashes):
            raise ValueError("Oracle-audit source hashes must be SHA-256 hex digests")


def load_oracle_stability_config(path: str | Path) -> OracleStabilityConfig:
    """Load an explicit, versioned stability protocol."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return OracleStabilityConfig(
        source_run_dir=str(payload["source_run_dir"]),
        candidate_result_path=str(payload["candidate_result_path"]),
        tail_confirmation_path=str(payload["tail_confirmation_path"]),
        results_dir=str(payload["results_dir"]),
        deployable_equivalence_groups=tuple(
            str(value) for value in payload["deployable_equivalence_groups"]
        ),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        confidence_level=float(payload["confidence_level"]),
        bootstrap_draws=int(payload["bootstrap_draws"]),
        bootstrap_seed=int(payload["bootstrap_seed"]),
        expected_measurements_sha256=str(payload["expected_measurements_sha256"]),
        expected_candidate_result_sha256=str(payload["expected_candidate_result_sha256"]),
        expected_tail_confirmation_sha256=str(payload["expected_tail_confirmation_sha256"]),
        require_clean_git=bool(payload["require_clean_git"]),
    )


def classify_ratio_interval(
    lower: float, upper: float, practical_tie_fraction: float
) -> PairConclusion:
    """Classify a ratio only when its full interval clears the tie band."""

    if upper < 1.0 / (1.0 + practical_tie_fraction):
        return "LEFT_MATERIALLY_FASTER"
    if lower > 1.0 + practical_tie_fraction:
        return "LEFT_MATERIALLY_SLOWER"
    return "NO_PRACTICAL_DOMINANCE_AUTHORIZED"


def confidence_undominated_set(
    candidate_ids: Sequence[str], pairwise_results: Sequence[Mapping[str, object]]
) -> tuple[str, ...]:
    """Keep candidates that no confidence-authorized comparison dominates."""

    dominated: set[str] = set()
    for result in pairwise_results:
        conclusion = str(result["conclusion"])
        if conclusion == "LEFT_MATERIALLY_FASTER":
            dominated.add(str(result["right_candidate_id"]))
        elif conclusion == "LEFT_MATERIALLY_SLOWER":
            dominated.add(str(result["left_candidate_id"]))
    remaining = tuple(sorted(set(candidate_ids) - dominated))
    if not remaining:
        raise ValueError("Confidence comparisons produced a dominance cycle")
    return remaining


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return cast(dict[str, Any], payload)


def _scenario_id(row: Mapping[str, str]) -> str:
    return f"n{row['row_count']}-w{row['identifier_width']}-m{row['match_rate']}"


def _stable_bootstrap_seed(base: int, label: str) -> int:
    digest = hashlib.sha256(f"{base}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _seed_oracle_sets(
    by_block: Mapping[tuple[int, int], Mapping[str, float]],
    candidate_ids: Sequence[str],
    tie: float,
) -> dict[int, tuple[str, ...]]:
    """Retain seed-level labels solely as an instability diagnostic."""

    by_seed: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (seed, _repeat), latencies in by_block.items():
        for candidate_id in candidate_ids:
            by_seed[seed][candidate_id].append(latencies[candidate_id])
    result: dict[int, tuple[str, ...]] = {}
    for seed, values in sorted(by_seed.items()):
        medians = {
            candidate_id: statistics.median(latencies) for candidate_id, latencies in values.items()
        }
        best = min(medians.values())
        result[seed] = tuple(
            sorted(
                candidate_id
                for candidate_id, latency in medians.items()
                if latency <= best * (1.0 + tie)
            )
        )
    return result


def _paired_family(
    family_key: tuple[str, str],
    rows: Sequence[Mapping[str, str]],
    candidate_ids: Sequence[str],
    config: OracleStabilityConfig,
    *,
    progress: Callable[[str], None] | None,
) -> dict[str, Any]:
    by_block: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
    digests: dict[tuple[int, int], set[str]] = defaultdict(set)
    for row in rows:
        block = (int(row["seed"]), int(row["repeat_index"]))
        by_block[block][row["variant_id"]] = float(row["latency_ms"])
        digests[block].add(row["result_digest"])
    if any(set(values) != set(candidate_ids) for values in by_block.values()):
        raise ValueError(f"Incomplete paired candidate block: {family_key}")
    if any(len(values) != 1 for values in digests.values()):
        raise ValueError(f"Result-equivalence failure: {family_key}")

    seed_sets = _seed_oracle_sets(by_block, candidate_ids, config.practical_tie_fraction)
    seed_ids = tuple(sorted(seed_sets))
    run_counts = Counter(seed for seed, _repeat in by_block)
    if len(seed_ids) < 3 or len(set(run_counts.values())) != 1:
        raise ValueError(f"Unbalanced seed/repetition matrix: {family_key}")

    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(candidate_ids):
        for right in candidate_ids[left_index + 1 :]:
            ratios: dict[int, list[float]] = defaultdict(list)
            for (seed, _repeat), latencies in sorted(by_block.items()):
                ratios[seed].append(math.log(latencies[left] / latencies[right]))
            label = f"{family_key}:{left}:{right}"
            point, lower, upper = hierarchical_paired_log_ratio_ci(
                ratios,
                confidence_level=config.confidence_level,
                repetitions=config.bootstrap_draws,
                seed=_stable_bootstrap_seed(config.bootstrap_seed, label),
            )
            pair = {
                "left_candidate_id": left,
                "right_candidate_id": right,
                "median_left_over_right_ratio": point,
                "confidence_interval": [lower, upper],
                "conclusion": classify_ratio_interval(lower, upper, config.practical_tie_fraction),
                "data_seed_count": len(ratios),
                "paired_round_count": sum(len(values) for values in ratios.values()),
            }
            pairs.append(pair)
            if progress is not None:
                progress(label)

    confidence_set = confidence_undominated_set(candidate_ids, pairs)
    seed_set_values = tuple(seed_sets.values())
    return {
        "scenario_id": family_key[0],
        "equivalence_group": family_key[1],
        "candidate_ids": list(candidate_ids),
        "confidence_undominated_candidate_ids": list(confidence_set),
        "confidence_set_is_singleton": len(confidence_set) == 1,
        "seed_oracle_sets": {str(seed): list(values) for seed, values in seed_sets.items()},
        "seed_oracle_sets_all_equal": len(set(seed_set_values)) == 1,
        "seed_oracle_union": sorted(set().union(*map(set, seed_set_values))),
        "seed_oracle_intersection": sorted(set.intersection(*map(set, seed_set_values))),
        "pairwise_results": pairs,
    }


def _selection_metrics(
    families: Sequence[Mapping[str, Any]],
    model_selections: Mapping[tuple[str, str], str],
    fixed_selections: Mapping[str, str],
) -> dict[str, Any]:
    model_hits = fixed_hits = singleton_count = 0
    model_singleton_hits = fixed_singleton_hits = 0
    decisions: list[dict[str, Any]] = []
    for family in families:
        key = (str(family["scenario_id"]), str(family["equivalence_group"]))
        confidence_set = set(cast(list[str], family["confidence_undominated_candidate_ids"]))
        model = model_selections[key]
        fixed = fixed_selections[key[1]]
        model_hit = model in confidence_set
        fixed_hit = fixed in confidence_set
        model_hits += model_hit
        fixed_hits += fixed_hit
        singleton = len(confidence_set) == 1
        singleton_count += singleton
        model_singleton_hits += singleton and model_hit
        fixed_singleton_hits += singleton and fixed_hit
        decisions.append(
            {
                "scenario_id": key[0],
                "equivalence_group": key[1],
                "confidence_oracle_set": sorted(confidence_set),
                "model_selection": model,
                "model_hit": model_hit,
                "fixed_selection": fixed,
                "fixed_hit": fixed_hit,
            }
        )
    count = len(families)
    return {
        "family_count": count,
        "singleton_confidence_winner_count": singleton_count,
        "model_confidence_set_hit_rate": model_hits / count,
        "fixed_confidence_set_hit_rate": fixed_hits / count,
        "model_confidence_authorized_miss_count": count - model_hits,
        "fixed_confidence_authorized_miss_count": count - fixed_hits,
        "model_singleton_winner_hit_rate": (
            model_singleton_hits / singleton_count if singleton_count else None
        ),
        "fixed_singleton_winner_hit_rate": (
            fixed_singleton_hits / singleton_count if singleton_count else None
        ),
        "decisions": decisions,
    }


def audit_execution_aware_oracle_stability(
    config: OracleStabilityConfig,
    *,
    project_root: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Audit all deployable development families without changing the model."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Oracle stability audit requires a clean Git commit")
    source_run = root / config.source_run_dir
    measurements_path = source_run / "measurements.csv"
    candidate_path = root / config.candidate_result_path
    tail_path = root / config.tail_confirmation_path
    actual_hashes = {
        "measurements": _sha256(measurements_path),
        "candidate_result": _sha256(candidate_path),
        "tail_confirmation": _sha256(tail_path),
    }
    expected_hashes = {
        "measurements": config.expected_measurements_sha256,
        "candidate_result": config.expected_candidate_result_sha256,
        "tail_confirmation": config.expected_tail_confirmation_sha256,
    }
    if actual_hashes != expected_hashes:
        raise ValueError("Oracle stability source hash mismatch")
    source_environment = _load_json(source_run / "environment.json")
    candidate_result = _load_json(candidate_path)
    tail_result = _load_json(tail_path)
    if source_environment.get("git_dirty") is not False:
        raise ValueError("Oracle stability source run was not clean")
    if tail_result.get("scientific_conclusion") != "CONFIRMED_FUSED_ADVANTAGE":
        raise ValueError("Tail confirmation conclusion changed")

    with measurements_path.open(newline="", encoding="utf-8") as handle:
        all_rows = list(csv.DictReader(handle))
    variants = {item.variant_id: item for item in execution_flow_variants()}
    groups = set(config.deployable_equivalence_groups)
    rows = [row for row in all_rows if row["equivalence_group"] in groups]
    family_rows: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        variant = variants[row["variant_id"]]
        if variant.evaluation_role != "deployable":
            raise ValueError("Mechanism-only candidate entered deployment audit")
        family_rows[(_scenario_id(row), row["equivalence_group"])].append(row)

    pair_total = sum(
        math.comb(len({row["variant_id"] for row in values}), 2) for values in family_rows.values()
    )
    pair_done = 0

    def progress(label: str) -> None:
        nonlocal pair_done
        pair_done += 1
        if progress_callback is not None:
            progress_callback(pair_done, pair_total, label)

    families: list[dict[str, Any]] = []
    for key, values in sorted(family_rows.items()):
        candidate_ids = tuple(sorted({row["variant_id"] for row in values}))
        families.append(_paired_family(key, values, candidate_ids, config, progress=progress))

    model_by_family: dict[tuple[str, str], set[str]] = defaultdict(set)
    decisions = cast(list[dict[str, Any]], candidate_result["grouped_validation"]["decisions"])
    for decision in decisions:
        model_by_family[(str(decision["scenario_id"]), str(decision["equivalence_group"]))].add(
            str(decision["selected_candidate_id"])
        )
    if set(model_by_family) != set(family_rows) or any(
        len(values) != 1 for values in model_by_family.values()
    ):
        raise ValueError("Model decisions do not define one seed-independent selection")
    model_selections = {key: next(iter(values)) for key, values in model_by_family.items()}
    metrics = _selection_metrics(families, model_selections, DEPLOYABLE_STABLE_PREFERENCES)
    unstable_seed_labels = sum(
        not bool(family["seed_oracle_sets_all_equal"]) for family in families
    )
    singleton_families = sum(bool(family["confidence_set_is_singleton"]) for family in families)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "status": "PASS_EXECUTION_AWARE_ORACLE_STABILITY_AUDIT",
        "analysis_commit_hash": commit,
        "analysis_git_dirty": dirty,
        "source_run_commit_hash": source_environment["commit_hash"],
        "source_hashes": actual_hashes,
        "source_candidate_status": candidate_result["status"],
        "tail_confirmation_conclusion": tail_result["scientific_conclusion"],
        "family_count": len(families),
        "pairwise_comparison_count": pair_total,
        "families_with_seed_oracle_disagreement": unstable_seed_labels,
        "singleton_confidence_winner_count": singleton_families,
        "multi_candidate_confidence_set_count": len(families) - singleton_families,
        "selection_metrics": metrics,
        "families": families,
        "inference": {
            "estimand": "median paired within-round latency ratio",
            "resampling": "complete data seeds, then paired rounds within seed",
            "confidence_level": config.confidence_level,
            "bootstrap_draws": config.bootstrap_draws,
            "practical_tie_fraction": config.practical_tie_fraction,
        },
        "optimizer_retrained": False,
        "old_failed_gate_relabelled": False,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This consumed-development audit measures label stability and reevaluates "
            "an unchanged model against confidence-undominated sets. It neither "
            "erases the frozen failed gate nor constitutes final holdout evidence."
        ),
        "config": asdict(config),
    }
    _atomic_json(output_dir / "oracle_stability_audit.json", result)
    _atomic_json(
        output_dir / "environment.json",
        {
            "captured_at": datetime.now(UTC).isoformat(),
            "analysis_commit_hash": commit,
            "analysis_git_dirty": dirty,
            "source_run_commit_hash": source_environment["commit_hash"],
        },
    )
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})
    return output_dir
