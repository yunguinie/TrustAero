"""Independent holdout evaluation for the frozen V3.1 uncertainty guard."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    _confidence_oracles,
    _load_statistics_and_medians,
    _p95,
    _sha256,
    analytic_model_from_dict,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
)
from trustaero.optimizer.governed_checkpoint_uncertainty import (
    CheckpointUncertaintyGuard,
    rank_uncertainty_aware_checkpoint_candidates,
)

PERMISSIVE_POLICY = GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None)
STRICT_POLICY = GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0)
OUT_OF_SUPPORT_REASON = "GOVERNED_CHECKPOINT_OUT_OF_SUPPORT_SAFE_FALLBACK"


@dataclass(frozen=True, slots=True)
class UncertaintyHoldoutConfig:
    """Frozen guard bindings, untouched dimensions, and result gates."""

    results_dir: str
    guard_path: str
    calibration_record_path: str
    expected_guard_sha256: str
    expected_calibration_sha256: str
    excluded_identifier_widths: tuple[int, ...]
    excluded_policy_selectivities: tuple[float, ...]
    excluded_query_selectivities: tuple[float, ...]
    excluded_seeds: tuple[int, ...]
    holdout_row_counts: tuple[int, ...]
    holdout_identifier_widths: tuple[int, ...]
    holdout_policy_selectivities: tuple[float, ...]
    holdout_query_selectivities: tuple[float, ...]
    holdout_seeds: tuple[int, ...]
    minimum_confidence_family_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_singleton_families: int
    minimum_policy_singleton_families: int
    minimum_query_singleton_families: int
    maximum_out_of_support_fallback_rate: float
    require_better_than_both_fixed: bool
    require_seed_consistency: bool
    require_clean_git: bool

    def __post_init__(self) -> None:
        comparisons = (
            (
                "identifier width",
                self.excluded_identifier_widths,
                self.holdout_identifier_widths,
            ),
            (
                "policy selectivity",
                self.excluded_policy_selectivities,
                self.holdout_policy_selectivities,
            ),
            (
                "query selectivity",
                self.excluded_query_selectivities,
                self.holdout_query_selectivities,
            ),
            ("seed", self.excluded_seeds, self.holdout_seeds),
        )
        for label, excluded, holdout in comparisons:
            overlap = set(excluded) & set(holdout)
            if overlap:
                raise ValueError(f"V3.1 holdout reuses consumed {label}: {overlap}")
        dimensions = (
            self.holdout_row_counts,
            self.holdout_identifier_widths,
            self.holdout_policy_selectivities,
            self.holdout_query_selectivities,
            self.holdout_seeds,
        )
        if any(not values or len(values) != len(set(values)) for values in dimensions):
            raise ValueError("V3.1 holdout dimensions must be nonempty and unique")
        rates = (
            self.minimum_confidence_family_hit_rate,
            self.maximum_out_of_support_fallback_rate,
        )
        if any(not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("V3.1 holdout rate gates must be in [0, 1]")


def load_uncertainty_holdout_config(path: str | Path) -> UncertaintyHoldoutConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    integer_tuples = (
        "excluded_identifier_widths",
        "excluded_seeds",
        "holdout_row_counts",
        "holdout_identifier_widths",
        "holdout_seeds",
    )
    float_tuples = (
        "excluded_policy_selectivities",
        "excluded_query_selectivities",
        "holdout_policy_selectivities",
        "holdout_query_selectivities",
    )
    for name in integer_tuples:
        payload[name] = tuple(int(value) for value in payload[name])
    for name in float_tuples:
        payload[name] = tuple(float(value) for value in payload[name])
    return UncertaintyHoldoutConfig(**payload)


def _load_guard(
    config: UncertaintyHoldoutConfig, root: Path
) -> tuple[CheckpointUncertaintyGuard, dict[str, str]]:
    guard_path = root / config.guard_path
    calibration_path = root / config.calibration_record_path
    hashes = {
        "guard": _sha256(guard_path),
        "development_calibration": _sha256(calibration_path),
    }
    expected = {
        "guard": config.expected_guard_sha256,
        "development_calibration": config.expected_calibration_sha256,
    }
    if hashes != expected:
        raise ValueError("Frozen V3.1 guard or calibration hash mismatch")
    payload = json.loads(guard_path.read_text(encoding="utf-8"))
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if calibration.get("status") != "PASS_EA1_CHECKPOINT_UNCERTAINTY_V31_DEVELOPMENT":
        raise ValueError("V3.1 guard lacks a passed development calibration")
    if calibration.get("analysis_git_dirty") is not False:
        raise ValueError("V3.1 guard was calibrated from a dirty tree")
    if calibration.get("failed_v2_holdout_used_for_numeric_calibration") is not False:
        raise ValueError("V3.1 guard illegally consumed the failed holdout")
    if calibration.get("guard") != payload:
        raise ValueError("Frozen V3.1 guard differs from its calibration record")
    if payload.get("model_type") != "uncertainty_aware_governed_checkpoint_v1":
        raise ValueError("Unknown V3.1 guard model type")
    guard = CheckpointUncertaintyGuard(
        base_model=analytic_model_from_dict(payload["base_model"]),
        query_margin_error_upper_ms=float(payload["query_margin_error_upper_ms"]),
        coverage=float(payload["coverage"]),
        calibration_family_count=int(payload["calibration_family_count"]),
        calibration_method=str(payload["calibration_method"]),
    )
    return guard, hashes


def _validate_measurement_run(
    config: UncertaintyHoldoutConfig, run_dir: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    run_config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_EA1_GOVERNED_CHECKPOINT_PILOT_INTEGRITY":
        raise ValueError("V3.1 holdout requires a passed timing run")
    if summary.get("experiment_role") != "frozen_optimizer_holdout":
        raise ValueError("Development timing cannot be relabelled as V3.1 holdout")
    if environment.get("git_dirty") is not False:
        raise ValueError("V3.1 holdout measurement used a dirty tree")
    expected_dimensions = {
        "row_counts": list(config.holdout_row_counts),
        "identifier_widths": list(config.holdout_identifier_widths),
        "policy_selectivities": list(config.holdout_policy_selectivities),
        "query_selectivities": list(config.holdout_query_selectivities),
        "seeds": list(config.holdout_seeds),
    }
    if any(run_config.get(name) != value for name, value in expected_dimensions.items()):
        raise ValueError("V3.1 holdout dimensions differ from the freeze")
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
        raise ValueError("V3.1 holdout scenario count is incomplete")
    return summary, run_config, environment


def evaluate_uncertainty_holdout(
    config: UncertaintyHoldoutConfig,
    *,
    source_run_dir: str | Path,
    project_root: Path,
) -> Path:
    """Evaluate a hash-bound V3.1 guard once without calibration or tuning."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("V3.1 holdout evaluation requires a clean Git commit")
    run_dir = Path(source_run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    summary, run_config, environment = _validate_measurement_run(config, run_dir)
    guard, frozen_hashes = _load_guard(config, root)
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
        raise ValueError("V3.1 holdout unit count is incomplete")

    decisions: list[dict[str, Any]] = []
    strict_violations = 0
    for (scenario_id, seed), planner_statistics in sorted(statistics_by_key.items()):
        ranking = rank_uncertainty_aware_checkpoint_candidates(
            planner_statistics, PERMISSIVE_POLICY, guard
        )
        selected = ranking.selected_candidate_id
        if selected is None:
            raise ValueError("V3.1 permissive holdout rejected every candidate")
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
                    item.candidate_id: item.total_ms for item in ranking.estimates
                },
            }
        )
        strict = rank_uncertainty_aware_checkpoint_candidates(
            planner_statistics, STRICT_POLICY, guard
        )
        strict_violations += strict.selected_candidate_id != POLICY_FIRST_CHECKPOINT

    family_results: list[dict[str, Any]] = []
    for scenario_id in sorted(oracles):
        selections = {
            str(row["selected_candidate_id"])
            for row in decisions
            if row["scenario_id"] == scenario_id
        }
        family_results.append(
            {
                "scenario_id": scenario_id,
                "selected_candidate_ids_across_seeds": sorted(selections),
                "seed_consistent": len(selections) == 1,
                "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                "confidence_oracle_hit": selections.issubset(set(oracles[scenario_id])),
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
    best_fixed = max(fixed_policy_hit, fixed_query_hit)
    metrics = {
        "confidence_family_hit_rate": family_hit,
        "seed_consistent_family_rate": seed_consistency,
        "mean_diagnostic_median_regret_percent": statistics.mean(regrets),
        "p95_diagnostic_median_regret_percent": _p95(regrets),
        "maximum_diagnostic_median_regret_percent": max(regrets),
        "fixed_policy_first_confidence_family_hit_rate": fixed_policy_hit,
        "fixed_query_first_confidence_family_hit_rate": fixed_query_hit,
        "best_fixed_confidence_family_hit_rate": best_fixed,
        "singleton_confidence_family_count": singleton_policy + singleton_query,
        "policy_first_singleton_family_count": singleton_policy,
        "query_first_singleton_family_count": singleton_query,
        "out_of_support_fallback_rate": out_of_support_rate,
        "strict_policy_illegal_selection_count": strict_violations,
        "reason_code_counts": dict(
            sorted(Counter(str(row["reason_code"]) for row in decisions).items())
        ),
    }
    gates = {
        "minimum_confidence_family_hit_rate": (
            family_hit >= config.minimum_confidence_family_hit_rate
        ),
        "better_than_both_fixed": (
            family_hit > best_fixed if config.require_better_than_both_fixed else True
        ),
        "seed_consistent_selection": (
            seed_consistency == 1.0 if config.require_seed_consistency else True
        ),
        "maximum_mean_regret": (
            metrics["mean_diagnostic_median_regret_percent"] <= config.maximum_mean_regret_percent
        ),
        "maximum_p95_regret": (
            metrics["p95_diagnostic_median_regret_percent"] <= config.maximum_p95_regret_percent
        ),
        "maximum_regret": (
            metrics["maximum_diagnostic_median_regret_percent"] <= config.maximum_regret_percent
        ),
        "minimum_singleton_families": (
            singleton_policy + singleton_query >= config.minimum_singleton_families
        ),
        "bidirectional_singleton_evidence": (
            singleton_policy >= config.minimum_policy_singleton_families
            and singleton_query >= config.minimum_query_singleton_families
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
            "PASS_EA1_V31_FROZEN_OPTIMIZER_HOLDOUT"
            if passed
            else "FAIL_EA1_V31_FROZEN_OPTIMIZER_HOLDOUT_RETAIN"
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
        "guard_refitted_during_holdout": False,
        "consumed_dimension_overlap": False,
        "metrics": metrics,
        "gate_checks": gates,
        "family_results": family_results,
        "decisions": decisions,
        "measurement_config": run_config,
        "evaluation_config": asdict(config),
        "independent_synthetic_holdout_claim_authorized": passed,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "Passing independently supports the controlled EA-1 V3.1 claim. "
            "Real-data and scale-transfer evidence remain separate requirements."
        ),
    }
    _atomic_json(output_dir / "evaluation.json", result)
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})
    return output_dir
