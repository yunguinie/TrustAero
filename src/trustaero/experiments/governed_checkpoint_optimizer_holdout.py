"""Independent evaluation of the frozen governed-checkpoint optimizer.

This module deliberately contains no fitting code.  It binds one committed
model by SHA-256, rejects overlap with the development dimensions, and scores
the already-frozen model against paired confidence sets from a fresh run.
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
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.execution_aware import (
    AnalyticExecutionCostModel,
    AnalyticFeatureRate,
    FeatureSupportBound,
)
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
    rank_governed_checkpoint_candidates,
)

PERMISSIVE_POLICY = GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None)
STRICT_POLICY = GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0)
OUT_OF_SUPPORT_REASON = "GOVERNED_CHECKPOINT_OUT_OF_SUPPORT_SAFE_FALLBACK"


@dataclass(frozen=True, slots=True)
class CheckpointHoldoutConfig:
    """Frozen bindings, unseen dimensions, and pass/fail thresholds."""

    results_dir: str
    model_path: str
    calibration_record_path: str
    expected_model_sha256: str
    expected_calibration_sha256: str
    expected_calibration_id: str
    development_identifier_widths: tuple[int, ...]
    development_policy_selectivities: tuple[float, ...]
    development_query_selectivities: tuple[float, ...]
    development_seeds: tuple[int, ...]
    holdout_row_counts: tuple[int, ...]
    holdout_identifier_widths: tuple[int, ...]
    holdout_policy_selectivities: tuple[float, ...]
    holdout_query_selectivities: tuple[float, ...]
    holdout_seeds: tuple[int, ...]
    minimum_confidence_family_hit_rate: float
    maximum_mean_diagnostic_regret_percent: float
    maximum_p95_diagnostic_regret_percent: float
    maximum_diagnostic_regret_percent: float
    minimum_singleton_confidence_families: int
    minimum_policy_first_singleton_families: int
    minimum_query_first_singleton_families: int
    maximum_out_of_support_fallback_rate: float
    require_better_than_both_fixed: bool
    require_seed_consistent_selection: bool
    require_clean_git: bool

    def __post_init__(self) -> None:
        sequences: tuple[tuple[object, ...], ...] = (
            cast(tuple[object, ...], self.holdout_row_counts),
            cast(tuple[object, ...], self.holdout_identifier_widths),
            cast(tuple[object, ...], self.holdout_policy_selectivities),
            cast(tuple[object, ...], self.holdout_query_selectivities),
            cast(tuple[object, ...], self.holdout_seeds),
        )
        if any(not values or len(values) != len(set(values)) for values in sequences):
            raise ValueError("Holdout dimensions must be nonempty and unique")
        rates = (
            self.minimum_confidence_family_hit_rate,
            self.maximum_out_of_support_fallback_rate,
        )
        if any(not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("Holdout rate gates must be in [0, 1]")
        regret_gates = (
            self.maximum_mean_diagnostic_regret_percent,
            self.maximum_p95_diagnostic_regret_percent,
            self.maximum_diagnostic_regret_percent,
        )
        if any(value < 0.0 or not math.isfinite(value) for value in regret_gates):
            raise ValueError("Holdout regret gates must be finite and nonnegative")
        singleton_gates = (
            self.minimum_singleton_confidence_families,
            self.minimum_policy_first_singleton_families,
            self.minimum_query_first_singleton_families,
        )
        if any(value < 0 for value in singleton_gates):
            raise ValueError("Holdout singleton gates must be nonnegative")
        validate_holdout_matrix(self)


def load_checkpoint_holdout_config(path: str | Path) -> CheckpointHoldoutConfig:
    """Load the pre-registered holdout evaluator configuration."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    tuple_fields = {
        "development_identifier_widths": int,
        "development_policy_selectivities": float,
        "development_query_selectivities": float,
        "development_seeds": int,
        "holdout_row_counts": int,
        "holdout_identifier_widths": int,
        "holdout_policy_selectivities": float,
        "holdout_query_selectivities": float,
        "holdout_seeds": int,
    }
    for field_name, converter in tuple_fields.items():
        payload[field_name] = tuple(converter(value) for value in payload[field_name])
    return CheckpointHoldoutConfig(**payload)


def validate_holdout_matrix(config: CheckpointHoldoutConfig) -> None:
    """Reject direct reuse of any tunable development dimension."""

    comparisons = (
        (
            "identifier width",
            config.development_identifier_widths,
            config.holdout_identifier_widths,
        ),
        (
            "policy selectivity",
            config.development_policy_selectivities,
            config.holdout_policy_selectivities,
        ),
        (
            "query selectivity",
            config.development_query_selectivities,
            config.holdout_query_selectivities,
        ),
        ("seed", config.development_seeds, config.holdout_seeds),
    )
    for label, development, holdout in comparisons:
        overlap = set(development) & set(holdout)
        if overlap:
            raise ValueError(f"Holdout reuses development {label}: {sorted(overlap)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def analytic_model_from_dict(payload: Mapping[str, Any]) -> AnalyticExecutionCostModel:
    """Reconstruct the analytic model without fitting or editing coefficients."""

    if payload.get("model_type") != "execution_aware_analytic_cost_v1":
        raise ValueError("Unsupported frozen checkpoint model type")
    if payload.get("direct_winner_classifier_used") is not False:
        raise ValueError("Holdout forbids a direct winner classifier")
    return AnalyticExecutionCostModel(
        calibration_id=str(payload["calibration_id"]),
        rates=tuple(
            AnalyticFeatureRate(str(item["feature_name"]), float(item["milliseconds_per_unit"]))
            for item in cast(list[dict[str, Any]], payload["rates"])
        ),
        support_bounds=tuple(
            FeatureSupportBound(
                str(item["feature_name"]),
                float(item["minimum"]),
                float(item["maximum"]),
            )
            for item in cast(list[dict[str, Any]], payload["support_bounds"])
        ),
        stable_legal_preference=tuple(str(value) for value in payload["stable_legal_preference"]),
        practical_tie_fraction=float(payload["practical_tie_fraction"]),
        intercept_ms=float(payload["intercept_ms"]),
    )


def _load_frozen_model(
    config: CheckpointHoldoutConfig, root: Path
) -> tuple[AnalyticExecutionCostModel, dict[str, str]]:
    model_path = root / config.model_path
    calibration_path = root / config.calibration_record_path
    hashes = {
        "model": _sha256(model_path),
        "development_calibration": _sha256(calibration_path),
    }
    expected = {
        "model": config.expected_model_sha256,
        "development_calibration": config.expected_calibration_sha256,
    }
    if hashes != expected:
        raise ValueError("Frozen checkpoint model or calibration hash mismatch")
    model_payload = json.loads(model_path.read_text(encoding="utf-8"))
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if calibration.get("status") != "PASS_EA1_CHECKPOINT_OPTIMIZER_DEVELOPMENT":
        raise ValueError("Frozen checkpoint model lacks a passed development record")
    if calibration.get("analysis_git_dirty") is not False:
        raise ValueError("Frozen checkpoint model was calibrated from a dirty tree")
    if calibration.get("final_model") != model_payload:
        raise ValueError("Frozen model content differs from its calibration record")
    model = analytic_model_from_dict(model_payload)
    if model.calibration_id != config.expected_calibration_id:
        raise ValueError("Frozen checkpoint calibration ID mismatch")
    return model, hashes


def _confidence_oracles(summary: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    oracles: dict[str, tuple[str, ...]] = {}
    for row in cast(list[dict[str, Any]], summary["scenario_results"]):
        conclusion = str(row["conclusion"])
        oracle: tuple[str, ...]
        if conclusion == "LEFT_MATERIALLY_FASTER":
            oracle = (POLICY_FIRST_CHECKPOINT,)
        elif conclusion == "LEFT_MATERIALLY_SLOWER":
            oracle = (QUERY_FIRST_CHECKPOINT,)
        elif conclusion == "NO_PRACTICAL_DOMINANCE_AUTHORIZED":
            oracle = (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
        else:
            raise ValueError(f"Unknown holdout confidence conclusion: {conclusion}")
        oracles[str(row["scenario_id"])] = oracle
    return oracles


def _validate_source_run(
    config: CheckpointHoldoutConfig, run_dir: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    run_config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY":
        raise ValueError("Holdout requires a passed paired-timing run")
    if summary.get("experiment_role") != "frozen_optimizer_holdout":
        raise ValueError("A development run cannot be relabelled as holdout")
    if run_config.get("experiment_role") != "frozen_optimizer_holdout":
        raise ValueError("Holdout run configuration has the wrong scientific role")
    if environment.get("git_dirty") is not False:
        raise ValueError("Holdout measurement must use a clean Git commit")
    expected_dimensions = {
        "row_counts": list(config.holdout_row_counts),
        "identifier_widths": list(config.holdout_identifier_widths),
        "policy_selectivities": list(config.holdout_policy_selectivities),
        "query_selectivities": list(config.holdout_query_selectivities),
        "seeds": list(config.holdout_seeds),
    }
    actual_dimensions = {name: run_config.get(name) for name in expected_dimensions}
    if actual_dimensions != expected_dimensions:
        raise ValueError("Holdout measurement dimensions differ from the freeze")
    expected_scenarios = math.prod(
        len(values)
        for values in (
            config.holdout_row_counts,
            config.holdout_identifier_widths,
            config.holdout_policy_selectivities,
            config.holdout_query_selectivities,
        )
    )
    if summary.get("scenario_count") != expected_scenarios:
        raise ValueError("Holdout scenario count is incomplete")
    return summary, run_config, environment


def _load_statistics_and_medians(
    run_dir: Path,
) -> tuple[
    dict[tuple[str, int], GovernedCheckpointStatistics],
    dict[tuple[str, int, str], float],
]:
    statistics_by_key: dict[tuple[str, int], GovernedCheckpointStatistics] = {}
    for path in sorted((run_dir / "units").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        unit = cast(dict[str, Any], payload["unit"])
        actual = cast(dict[str, Any], payload["actual_cardinalities"])
        seed = int(unit["seed"])
        unit_id = str(payload["unit_id"])
        suffix = f"-s{seed}"
        if not unit_id.endswith(suffix):
            raise ValueError(f"Holdout unit is not seed-bound: {unit_id}")
        key = (unit_id[: -len(suffix)], seed)
        if key in statistics_by_key:
            raise ValueError(f"Duplicate holdout unit: {key}")
        statistics_by_key[key] = GovernedCheckpointStatistics(
            input_rows=int(unit["row_count"]),
            sensitive_width_bytes=float(unit["identifier_width"]),
            estimated_policy_rows=int(actual["policy_rows"]),
            estimated_query_rows=int(actual["query_rows"]),
            estimated_result_rows=int(actual["result_rows"]),
            statistic_provenance="catalog_exact_controlled",
        )
    samples: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    with (run_dir / "measurements.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            samples[(row["scenario_id"], int(row["seed"]), row["candidate_id"])].append(
                float(row["latency_ms"])
            )
    medians = {key: statistics.median(values) for key, values in samples.items()}
    expected_keys = {
        (scenario_id, seed, candidate_id)
        for scenario_id, seed in statistics_by_key
        for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
    }
    if set(medians) != expected_keys:
        raise ValueError("Holdout measurements are incomplete or contain extra candidates")
    return statistics_by_key, medians


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def evaluate_governed_checkpoint_optimizer_holdout(
    config: CheckpointHoldoutConfig,
    *,
    source_run_dir: str | Path,
    project_root: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Score one frozen model once; this function never calibrates a model."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Holdout evaluation requires a clean Git commit")
    run_dir = Path(source_run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    summary, run_config, environment = _validate_source_run(config, run_dir)
    model, frozen_hashes = _load_frozen_model(config, root)
    oracles = _confidence_oracles(summary)
    statistics_by_key, medians = _load_statistics_and_medians(run_dir)
    expected_units = math.prod(
        len(values)
        for values in (
            config.holdout_row_counts,
            config.holdout_identifier_widths,
            config.holdout_policy_selectivities,
            config.holdout_query_selectivities,
            config.holdout_seeds,
        )
    )
    if len(statistics_by_key) != expected_units:
        raise ValueError("Holdout unit count is incomplete")
    if set(oracles) != {scenario_id for scenario_id, _seed in statistics_by_key}:
        raise ValueError("Holdout confidence families do not match measured units")

    decisions: list[dict[str, Any]] = []
    strict_violations = 0
    items = sorted(statistics_by_key.items())
    for index, ((scenario_id, seed), planner_statistics) in enumerate(items, start=1):
        ranking = rank_governed_checkpoint_candidates(planner_statistics, PERMISSIVE_POLICY, model)
        selected = ranking.selected_candidate_id
        if selected is None:
            raise ValueError("Permissive holdout ranking rejected every candidate")
        actual = {
            candidate_id: medians[(scenario_id, seed, candidate_id)]
            for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
        }
        best = min(actual.values())
        decisions.append(
            {
                "scenario_id": scenario_id,
                "seed": seed,
                "selected_candidate_id": selected,
                "reason_code": ranking.reason_code,
                "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                "confidence_oracle_hit": selected in oracles[scenario_id],
                "diagnostic_median_regret_percent": (actual[selected] / best - 1.0) * 100.0,
                "actual_median_latency_ms": actual,
                "estimated_costs_ms": {
                    estimate.candidate_id: estimate.total_ms for estimate in ranking.estimates
                },
            }
        )
        strict = rank_governed_checkpoint_candidates(planner_statistics, STRICT_POLICY, model)
        strict_violations += strict.selected_candidate_id != POLICY_FIRST_CHECKPOINT
        if progress_callback is not None:
            progress_callback(index, len(items), f"{scenario_id}-s{seed}")

    family_results: list[dict[str, Any]] = []
    for scenario_id in sorted(oracles):
        selected_ids = {
            str(row["selected_candidate_id"])
            for row in decisions
            if row["scenario_id"] == scenario_id
        }
        family_results.append(
            {
                "scenario_id": scenario_id,
                "selected_candidate_ids_across_seeds": sorted(selected_ids),
                "seed_consistent": len(selected_ids) == 1,
                "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                "confidence_oracle_hit": selected_ids.issubset(set(oracles[scenario_id])),
            }
        )

    regrets = [float(row["diagnostic_median_regret_percent"]) for row in decisions]
    family_hit = statistics.mean(bool(row["confidence_oracle_hit"]) for row in family_results)
    seed_consistency = statistics.mean(bool(row["seed_consistent"]) for row in family_results)
    fixed_policy_hit = statistics.mean(
        POLICY_FIRST_CHECKPOINT in oracle for oracle in oracles.values()
    )
    fixed_query_hit = statistics.mean(
        QUERY_FIRST_CHECKPOINT in oracle for oracle in oracles.values()
    )
    singleton_policy = sum(oracle == (POLICY_FIRST_CHECKPOINT,) for oracle in oracles.values())
    singleton_query = sum(oracle == (QUERY_FIRST_CHECKPOINT,) for oracle in oracles.values())
    out_of_support = sum(row["reason_code"] == OUT_OF_SUPPORT_REASON for row in decisions)
    out_of_support_rate = out_of_support / len(decisions)
    reason_counts = Counter(str(row["reason_code"]) for row in decisions)
    selection_counts = Counter(str(row["selected_candidate_id"]) for row in decisions)
    best_fixed_hit = max(fixed_policy_hit, fixed_query_hit)
    metrics = {
        "confidence_family_hit_rate": family_hit,
        "seed_decision_confidence_hit_rate": statistics.mean(
            bool(row["confidence_oracle_hit"]) for row in decisions
        ),
        "seed_consistent_family_rate": seed_consistency,
        "mean_diagnostic_median_regret_percent": statistics.mean(regrets),
        "p95_diagnostic_median_regret_percent": _p95(regrets),
        "maximum_diagnostic_median_regret_percent": max(regrets),
        "fixed_policy_first_confidence_family_hit_rate": fixed_policy_hit,
        "fixed_query_first_confidence_family_hit_rate": fixed_query_hit,
        "best_fixed_confidence_family_hit_rate": best_fixed_hit,
        "singleton_confidence_family_count": singleton_policy + singleton_query,
        "policy_first_singleton_family_count": singleton_policy,
        "query_first_singleton_family_count": singleton_query,
        "out_of_support_fallback_count": out_of_support,
        "out_of_support_fallback_rate": out_of_support_rate,
        "strict_policy_illegal_selection_count": strict_violations,
        "reason_code_counts": dict(sorted(reason_counts.items())),
        "selected_candidate_counts": dict(sorted(selection_counts.items())),
    }
    gates = {
        "minimum_confidence_family_hit_rate": (
            family_hit >= config.minimum_confidence_family_hit_rate
        ),
        "better_than_both_fixed": (
            family_hit > best_fixed_hit if config.require_better_than_both_fixed else True
        ),
        "seed_consistent_selection": (
            seed_consistency == 1.0 if config.require_seed_consistent_selection else True
        ),
        "maximum_mean_diagnostic_regret": (
            metrics["mean_diagnostic_median_regret_percent"]
            <= config.maximum_mean_diagnostic_regret_percent
        ),
        "maximum_p95_diagnostic_regret": (
            metrics["p95_diagnostic_median_regret_percent"]
            <= config.maximum_p95_diagnostic_regret_percent
        ),
        "maximum_diagnostic_regret": (
            metrics["maximum_diagnostic_median_regret_percent"]
            <= config.maximum_diagnostic_regret_percent
        ),
        "minimum_singleton_confidence_families": (
            singleton_policy + singleton_query >= config.minimum_singleton_confidence_families
        ),
        "bidirectional_singleton_evidence": (
            singleton_policy >= config.minimum_policy_first_singleton_families
            and singleton_query >= config.minimum_query_first_singleton_families
        ),
        "maximum_out_of_support_fallback_rate": (
            out_of_support_rate <= config.maximum_out_of_support_fallback_rate
        ),
        "zero_illegal_selections": strict_violations == 0,
    }
    passed = all(gates.values())
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": (
            "PASS_EA1_FROZEN_OPTIMIZER_INTERPOLATION_HOLDOUT"
            if passed
            else "FAIL_EA1_FROZEN_OPTIMIZER_HOLDOUT_RETAIN"
        ),
        "analysis_commit_hash": commit,
        "analysis_git_dirty": dirty,
        "measurement_commit_hash": environment.get("commit_hash"),
        "source_run_dir": str(run_dir),
        "source_hashes": {
            "summary": _sha256(run_dir / "summary.json"),
            "measurements": _sha256(run_dir / "measurements.csv"),
            "config": _sha256(run_dir / "config.json"),
        },
        "frozen_artifact_hashes": frozen_hashes,
        "model_calibration_id": model.calibration_id,
        "model_refitted_during_holdout": False,
        "direct_winner_classifier_used": False,
        "development_dimension_overlap": False,
        "metrics": metrics,
        "gate_checks": gates,
        "family_results": family_results,
        "decisions": decisions,
        "measurement_config": run_config,
        "evaluation_config": asdict(config),
        "independent_synthetic_holdout_claim_authorized": passed,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "This frozen interpolation holdout can independently support the "
            "controlled EA-1 optimizer claim when all gates pass. Real-data and "
            "scale-transfer evidence remain separate requirements for the paper."
        ),
    }
    _atomic_json(output_dir / "evaluation.json", result)
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})
    return output_dir
