"""Independent real-month validation for the frozen checkpoint V4 model."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.governed_checkpoint_optimizer_calibration import (
    _confidence_oracles,
    _sha256,
)
from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    analytic_model_from_dict,
)
from trustaero.experiments.real_checkpoint_optimizer_calibration import _metrics
from trustaero.experiments.real_governed_checkpoint_transfer import (
    _real_statistics_and_medians,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_checkpoint import (
    POLICY_FIRST_CHECKPOINT,
    QUERY_FIRST_CHECKPOINT,
    GovernedCheckpointStatistics,
    PracticalTieStrategy,
    rank_governed_checkpoint_candidates,
)


@dataclass(frozen=True, slots=True)
class RealCheckpointValidationConfig:
    """Frozen artifacts, baseline, and validation-only acceptance gates."""

    results_dir: str
    model_path: str
    calibration_record_path: str
    measurement_config_path: str
    expected_model_sha256: str
    expected_calibration_sha256: str
    expected_measurement_config_sha256: str
    frozen_query_selectivity_threshold: float
    minimum_analytic_confidence_family_hit_rate: float
    maximum_analytic_mean_regret_percent: float
    maximum_analytic_p95_regret_percent: float
    maximum_analytic_regret_percent: float
    require_analytic_family_hit_no_worse_than_threshold: bool
    require_analytic_mean_regret_no_worse_than_threshold: bool
    require_seed_consistency: bool
    maximum_out_of_support_fallback_rate: float
    require_clean_git: bool
    optimizer_version: str = "V4"
    practical_tie_strategy: PracticalTieStrategy = "policy_first_fallback"
    expected_measurement_status: str = "PASS_EA1_REAL_OPTIMIZER_VALIDATION_MEASUREMENT_INTEGRITY"
    authorize_final_holdout_claim: bool = False
    minimum_analytic_family_hit_improvement_over_threshold: float = 0.0
    minimum_analytic_mean_regret_reduction_vs_threshold_percent: float = 0.0

    def __post_init__(self) -> None:
        fractions = (
            self.frozen_query_selectivity_threshold,
            self.minimum_analytic_confidence_family_hit_rate,
            self.maximum_out_of_support_fallback_rate,
        )
        if any(not 0.0 <= value <= 1.0 for value in fractions):
            raise ValueError("Real V4 validation fractions are invalid")
        if any(
            len(value) != 64
            for value in (
                self.expected_model_sha256,
                self.expected_calibration_sha256,
                self.expected_measurement_config_sha256,
            )
        ):
            raise ValueError("Real V4 validation hashes must be SHA-256")
        if self.optimizer_version not in {"V4", "V41"}:
            raise ValueError("Unknown real checkpoint optimizer version")
        if self.practical_tie_strategy not in (
            "policy_first_fallback",
            "minimum_analytic_cost",
        ):
            raise ValueError("Unknown real checkpoint practical-tie strategy")


def load_real_checkpoint_validation_config(
    path: str | Path,
) -> RealCheckpointValidationConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return RealCheckpointValidationConfig(**payload)


def threshold_candidate(statistics: GovernedCheckpointStatistics, threshold: float) -> str:
    """Apply the frozen strong baseline without looking at validation labels."""

    query_rate = statistics.estimated_query_rows / statistics.input_rows
    return QUERY_FIRST_CHECKPOINT if query_rate < threshold else POLICY_FIRST_CHECKPOINT


def _load_frozen_model(
    config: RealCheckpointValidationConfig, root: Path
) -> tuple[Any, dict[str, str]]:
    model_path = root / config.model_path
    calibration_path = root / config.calibration_record_path
    measurement_config_path = root / config.measurement_config_path
    hashes = {
        "model": _sha256(model_path),
        "development_calibration": _sha256(calibration_path),
        "measurement_config": _sha256(measurement_config_path),
    }
    if hashes != {
        "model": config.expected_model_sha256,
        "development_calibration": config.expected_calibration_sha256,
        "measurement_config": config.expected_measurement_config_sha256,
    }:
        raise ValueError("Frozen V4 validation artifact hash mismatch")
    model_payload = json.loads(model_path.read_text(encoding="utf-8"))
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if calibration.get("status") != "PASS_REAL_CHECKPOINT_OPTIMIZER_V4_DEVELOPMENT":
        raise ValueError("Frozen V4 lacks a passed development calibration")
    if calibration.get("analysis_git_dirty") is not False:
        raise ValueError("Frozen V4 development calibration used a dirty tree")
    if calibration.get("final_model") != model_payload:
        raise ValueError("Frozen V4 model differs from its development record")
    return analytic_model_from_dict(model_payload), hashes


def evaluate_real_checkpoint_optimizer_validation(
    config: RealCheckpointValidationConfig,
    *,
    source_run_dir: str | Path,
    project_root: Path,
) -> Path:
    """Evaluate V4 once; this function performs no fitting or threshold search."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Real V4 validation requires a clean Git commit")
    run_dir = Path(source_run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    run_config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    expected_measurement_config = json.loads(
        (root / config.measurement_config_path).read_text(encoding="utf-8")
    )
    if summary.get("status") != config.expected_measurement_status:
        raise ValueError("Real optimizer measurement integrity did not pass")
    if run_config != expected_measurement_config:
        raise ValueError("Real V4 validation measurement matrix changed")
    if environment.get("git_dirty") is not False:
        raise ValueError("Real V4 validation measurement used a dirty tree")
    model, frozen_hashes = _load_frozen_model(config, root)
    oracles = _confidence_oracles(summary)
    statistics_by_key, medians = _real_statistics_and_medians(run_dir)
    permissive = GovernanceFeasibilityPolicy("raw_checkpoint_permitted", None, None)
    strict = GovernanceFeasibilityPolicy("raw_checkpoint_forbidden", None, 0)
    decisions: list[dict[str, object]] = []
    strict_violations = 0
    out_of_support = 0
    for (scenario_id, seed), planner_statistics in sorted(statistics_by_key.items()):
        ranking = rank_governed_checkpoint_candidates(
            planner_statistics,
            permissive,
            model,
            practical_tie_strategy=config.practical_tie_strategy,
        )
        analytic_selected = ranking.selected_candidate_id
        if analytic_selected is None:
            raise ValueError("Frozen V4 rejected every permissive candidate")
        threshold_selected = threshold_candidate(
            planner_statistics, config.frozen_query_selectivity_threshold
        )
        strict_ranking = rank_governed_checkpoint_candidates(
            planner_statistics,
            strict,
            model,
            practical_tie_strategy=config.practical_tie_strategy,
        )
        strict_violations += strict_ranking.selected_candidate_id != POLICY_FIRST_CHECKPOINT
        out_of_support += ranking.reason_code == "GOVERNED_CHECKPOINT_OUT_OF_SUPPORT_SAFE_FALLBACK"
        actual = {
            candidate_id: medians[(scenario_id, seed, candidate_id)]
            for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
        }
        best = min(actual.values())
        decisions.append(
            {
                "scenario_id": scenario_id,
                "seed": seed,
                "analytic_selected_candidate_id": analytic_selected,
                "analytic_reason_code": ranking.reason_code,
                "analytic_regret_percent": (actual[analytic_selected] / best - 1.0) * 100.0,
                "threshold_selected_candidate_id": threshold_selected,
                "threshold_regret_percent": (actual[threshold_selected] / best - 1.0) * 100.0,
                "confidence_oracle_candidate_ids": list(oracles[scenario_id]),
                "estimated_costs_ms": {
                    estimate.candidate_id: estimate.total_ms for estimate in ranking.estimates
                },
            }
        )

    analytic_metrics = _metrics(
        decisions,
        selection_field="analytic_selected_candidate_id",
        regret_field="analytic_regret_percent",
        oracles=oracles,
    )
    threshold_metrics = _metrics(
        decisions,
        selection_field="threshold_selected_candidate_id",
        regret_field="threshold_regret_percent",
        oracles=oracles,
    )
    fixed_metrics = {}
    for candidate_id in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT):
        fixed_decisions = []
        for row in decisions:
            scenario_id = str(row["scenario_id"])
            seed = int(cast(int, row["seed"]))
            actual = {
                candidate: medians[(scenario_id, seed, candidate)]
                for candidate in (POLICY_FIRST_CHECKPOINT, QUERY_FIRST_CHECKPOINT)
            }
            fixed_decisions.append(
                {
                    **row,
                    "fixed_selected_candidate_id": candidate_id,
                    "fixed_regret_percent": (actual[candidate_id] / min(actual.values()) - 1.0)
                    * 100.0,
                }
            )
        fixed_metrics[candidate_id] = _metrics(
            fixed_decisions,
            selection_field="fixed_selected_candidate_id",
            regret_field="fixed_regret_percent",
            oracles=oracles,
        )

    analytic_hit = float(cast(float, analytic_metrics["confidence_family_hit_rate"]))
    analytic_mean = float(cast(float, analytic_metrics["mean_regret_percent"]))
    analytic_p95 = float(cast(float, analytic_metrics["p95_regret_percent"]))
    analytic_max = float(cast(float, analytic_metrics["max_regret_percent"]))
    threshold_hit = float(cast(float, threshold_metrics["confidence_family_hit_rate"]))
    threshold_mean = float(cast(float, threshold_metrics["mean_regret_percent"]))
    fallback_rate = out_of_support / len(decisions)
    gates = {
        "minimum_analytic_confidence_family_hit_rate": analytic_hit
        >= config.minimum_analytic_confidence_family_hit_rate,
        "maximum_analytic_mean_regret_percent": analytic_mean
        <= config.maximum_analytic_mean_regret_percent,
        "maximum_analytic_p95_regret_percent": analytic_p95
        <= config.maximum_analytic_p95_regret_percent,
        "maximum_analytic_regret_percent": analytic_max <= config.maximum_analytic_regret_percent,
        "analytic_family_hit_no_worse_than_threshold": (
            analytic_hit >= threshold_hit
            or not config.require_analytic_family_hit_no_worse_than_threshold
        ),
        "analytic_mean_regret_no_worse_than_threshold": (
            analytic_mean <= threshold_mean + 1e-12
            or not config.require_analytic_mean_regret_no_worse_than_threshold
        ),
        "minimum_analytic_family_hit_improvement_over_threshold": (
            analytic_hit - threshold_hit
            >= config.minimum_analytic_family_hit_improvement_over_threshold
        ),
        "minimum_analytic_mean_regret_reduction_vs_threshold_percent": (
            threshold_mean - analytic_mean
            >= config.minimum_analytic_mean_regret_reduction_vs_threshold_percent
        ),
        "seed_consistency": (
            float(cast(float, analytic_metrics["seed_consistent_family_rate"])) == 1.0
            or not config.require_seed_consistency
        ),
        "out_of_support_fallback_rate": fallback_rate
        <= config.maximum_out_of_support_fallback_rate,
        "governance_legality": strict_violations == 0,
    }
    singleton_counts = Counter(tuple(values) for values in oracles.values() if len(values) == 1)
    passed = all(gates.values())
    evaluation_phase = "FINAL_HOLDOUT" if config.authorize_final_holdout_claim else "VALIDATION"
    status_prefix = f"REAL_CHECKPOINT_OPTIMIZER_{config.optimizer_version}_{evaluation_phase}"
    result = {
        "status": (f"PASS_{status_prefix}" if passed else f"FAIL_{status_prefix}_RETAIN"),
        "optimizer_version": config.optimizer_version,
        "practical_tie_strategy": config.practical_tie_strategy,
        "analytic_metrics": analytic_metrics,
        "frozen_threshold_metrics": threshold_metrics,
        "fixed_metrics": fixed_metrics,
        "gates": gates,
        "singleton_oracle_counts": {
            "policy_first": singleton_counts[(POLICY_FIRST_CHECKPOINT,)],
            "query_first": singleton_counts[(QUERY_FIRST_CHECKPOINT,)],
        },
        "out_of_support_fallback_rate": fallback_rate,
        "strict_policy_illegal_selection_count": strict_violations,
        "reason_counts": dict(Counter(str(row["analytic_reason_code"]) for row in decisions)),
        "decisions": decisions,
        "frozen_hashes": frozen_hashes,
        "measurement_run": str(run_dir.relative_to(root)),
        "measurement_commit_hash": environment["commit_hash"],
        "evaluation_commit_hash": commit,
        "evaluation_git_dirty": dirty,
        "validation_claim_authorized": passed,
        "final_holdout_claim_authorized": (passed and config.authorize_final_holdout_claim),
        "paper_optimizer_performance_claim_authorized": (
            passed and config.authorize_final_holdout_claim
        ),
        "scientific_boundary": (
            "Passing authorizes the pre-registered V4.1 untouched-month optimizer "
            "claim; it does not authorize claims for other queries, scales, or engines."
            if config.authorize_final_holdout_claim
            else "Passing authorizes freezing the optimizer for untouched-month final "
            "holdout. This validation is not itself a final paper performance claim."
        ),
    }
    output_root = root / config.results_dir
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(output_dir / "evaluation.json", result)
    _atomic_json(output_root / "latest_run.json", {"run_id": run_id})
    return output_dir
