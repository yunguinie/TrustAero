"""One-shot real holdout across governance-defined legal plan spaces.

The same physical observations are evaluated under three pre-registered policy
regimes.  Hard feasibility is always applied before the frozen cost model:

* permissive permits every checkpoint candidate;
* no-raw-join removes candidates that expose raw values to Join;
* strict also forbids raw-value materialization.

Only the no-raw-join regime authorizes the adaptive performance claim because
it retains two legal candidates whose winner may change with physical work.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.experiments.governed_pipeline_cost_calibration import (
    EQUIVALENCE_GROUP,
    _selection_metrics,
    fixed_candidate_baselines,
)
from trustaero.experiments.real_governed_pipeline_transfer import (
    _load_real_observations,
    _sha256,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_pipeline_cost import (
    FROZEN_V2_SUPPORT,
    ONLY_LEGAL_NONDOMINATED_CANDIDATE,
    OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK,
    FrozenGovernedPipelineCostModel,
    optimize_governed_pipeline,
)
from trustaero.optimizer.governed_pipeline_space import (
    POLICY_FIRST_MASKED_CHECKPOINT,
    GovernedPipelineStatistics,
)


@dataclass(frozen=True, slots=True)
class FrozenPolicyRegime:
    """One governance regime and its expected legal-space cardinality."""

    policy_id: str
    max_raw_join_rows: int | None
    max_raw_materialized_rows: int | None
    require_governance_checkpoint: bool
    expected_legal_candidate_count: int
    adaptive_performance_claim: bool

    def to_policy(self) -> GovernanceFeasibilityPolicy:
        """Construct the core policy object used by the production optimizer."""

        return GovernanceFeasibilityPolicy(
            self.policy_id,
            self.max_raw_join_rows,
            self.max_raw_materialized_rows,
            require_governance_checkpoint=self.require_governance_checkpoint,
        )


@dataclass(frozen=True, slots=True)
class PolicyStratifiedHoldoutConfig:
    """Frozen model, real split, policy regimes, and one-shot gates."""

    results_dir: str
    measurement_results_dir: str
    model_path: str
    model_sha256: str
    development_calibration_path: str
    development_calibration_sha256: str
    development_real_summary_path: str
    development_real_summary_sha256: str
    development_real_evaluation_path: str
    development_real_evaluation_sha256: str
    expected_sources: tuple[str, ...]
    expected_profiles: tuple[str, ...]
    expected_row_count: int
    expected_seeds: tuple[int, ...]
    policy_regimes: tuple[FrozenPolicyRegime, ...]
    primary_adaptive_policy_id: str
    minimum_oracle_set_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_p95_regret_percent: float
    maximum_regret_percent: float
    minimum_selected_candidate_count: int
    require_better_than_best_fixed_mean: bool
    require_better_than_best_fixed_p95: bool
    require_no_material_carryover: bool
    support_path: str | None = None
    support_sha256: str | None = None
    prior_holdout_negative_path: str | None = None
    prior_holdout_negative_sha256: str | None = None
    maximum_out_of_support_fallback_rate: float = 1.0

    def __post_init__(self) -> None:
        ids = tuple(item.policy_id for item in self.policy_regimes)
        if len(ids) != len(set(ids)) or self.primary_adaptive_policy_id not in ids:
            raise ValueError("Policy regimes must be unique and contain the primary regime")
        primary = next(
            item
            for item in self.policy_regimes
            if item.policy_id == self.primary_adaptive_policy_id
        )
        if not primary.adaptive_performance_claim:
            raise ValueError("Primary policy must authorize adaptive evaluation")
        if not 0.0 <= self.maximum_out_of_support_fallback_rate <= 1.0:
            raise ValueError("Maximum fallback rate must be in [0, 1]")
        optional_bindings = (
            (self.support_path, self.support_sha256),
            (
                self.prior_holdout_negative_path,
                self.prior_holdout_negative_sha256,
            ),
        )
        if any((path is None) != (digest is None) for path, digest in optional_bindings):
            raise ValueError("Optional frozen paths and digests must be paired")


def load_policy_stratified_holdout_config(
    path: Path | str,
) -> PolicyStratifiedHoldoutConfig:
    """Load a hash-bound policy-stratified holdout configuration."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["expected_sources"] = tuple(payload["expected_sources"])
    payload["expected_profiles"] = tuple(payload["expected_profiles"])
    payload["expected_seeds"] = tuple(int(value) for value in payload["expected_seeds"])
    payload["policy_regimes"] = tuple(
        FrozenPolicyRegime(**item) for item in payload["policy_regimes"]
    )
    return PolicyStratifiedHoldoutConfig(**payload)


def _statistics_by_group(
    run_dir: Path,
) -> dict[tuple[str, int, str], GovernedPipelineStatistics]:
    """Reconstruct trusted physical cardinalities from immutable unit records."""

    result: dict[tuple[str, int, str], GovernedPipelineStatistics] = {}
    for path in sorted((run_dir / "units").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        unit = payload["unit"]
        actual = payload["actual_cardinalities"]
        scenario_id = str(payload["measurements"][0]["scenario_id"])
        key = (scenario_id, int(unit["seed"]), EQUIVALENCE_GROUP)
        if key in result:
            raise ValueError(f"Duplicate real holdout group: {key}")
        result[key] = GovernedPipelineStatistics(
            input_rows=int(unit["row_count"]),
            estimated_policy_rows=int(actual["policy_rows"]),
            estimated_query_rows=int(actual["query_rows"]),
            estimated_governed_rows=int(actual["governed_rows"]),
            estimated_query_join_rows=int(actual["query_join_rows"]),
            estimated_result_rows=int(actual["result_rows"]),
            sensitive_width_bytes=float(unit["identifier_width"]),
        )
    if not result:
        raise ValueError("Policy-stratified holdout has no unit statistics")
    return result


def _validate_split(
    config: PolicyStratifiedHoldoutConfig,
    measured: dict[str, Any],
) -> None:
    source_ids = tuple(f"{item['dataset']}-{item['month']}" for item in measured["sources"])
    profile_ids = tuple(item["profile_id"] for item in measured["profiles"])
    if source_ids != config.expected_sources:
        raise ValueError("Policy-stratified source split changed")
    if profile_ids != config.expected_profiles:
        raise ValueError("Policy-stratified profile split changed")
    if int(measured["row_count"]) != config.expected_row_count:
        raise ValueError("Policy-stratified row count changed")
    if tuple(measured["seeds"]) != config.expected_seeds:
        raise ValueError("Policy-stratified seeds changed")


def evaluate_policy_stratified_pipeline_holdout(
    config: PolicyStratifiedHoldoutConfig,
    *,
    project_root: Path,
    measurement_run_dir: Path,
) -> Path:
    """Evaluate the frozen production optimizer without fitting or tuning."""

    root = project_root.resolve()
    run_dir = measurement_run_dir.resolve()
    bindings = {
        root / config.model_path: config.model_sha256,
        root / config.development_calibration_path: (config.development_calibration_sha256),
        root / config.development_real_summary_path: (config.development_real_summary_sha256),
        root / config.development_real_evaluation_path: (config.development_real_evaluation_sha256),
    }
    for path, expected in bindings.items():
        if _sha256(path) != expected:
            raise ValueError(f"Frozen prerequisite digest changed: {path}")
    if config.support_path is not None and config.support_sha256 is not None:
        support_path = root / config.support_path
        if _sha256(support_path) != config.support_sha256:
            raise ValueError("Frozen support artifact digest changed")
        support_payload = json.loads(support_path.read_text(encoding="utf-8"))
        tolerance = support_payload["finite_sample_tolerance"]
        if (
            tolerance["method"] != "two-sided Hoeffding bound"
            or float(tolerance["tail_probability"])
            != FROZEN_V2_SUPPORT.selectivity_tail_probability
        ):
            raise ValueError("Runtime support differs from frozen support artifact")
    if (
        config.prior_holdout_negative_path is not None
        and config.prior_holdout_negative_sha256 is not None
    ):
        negative_path = root / config.prior_holdout_negative_path
        if _sha256(negative_path) != config.prior_holdout_negative_sha256:
            raise ValueError("Prior negative holdout record changed")
    model = FrozenGovernedPipelineCostModel.from_json(
        root / config.model_path,
        expected_sha256=config.model_sha256,
    )
    measured = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    _validate_split(config, measured)
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    if environment.get("git_dirty") is not False:
        raise ValueError("Policy-stratified measurement was not run from a clean commit")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    carryover_passed = bool(summary["gate_checks"]["no_material_carryover"])

    observations, _permissive_legal, integrity = _load_real_observations(run_dir)
    statistics_by_group = _statistics_by_group(run_dir)
    observations_by_group: dict[tuple[str, int, str], list[Any]] = {}
    for observation in observations:
        key = (
            observation.scenario_id,
            observation.seed,
            observation.equivalence_group,
        )
        observations_by_group.setdefault(key, []).append(observation)
    if set(observations_by_group) != set(statistics_by_group):
        raise ValueError("Real observations and cardinality groups differ")

    regime_results: dict[str, dict[str, object]] = {}
    all_illegal: list[dict[str, object]] = []
    for regime in config.policy_regimes:
        policy = regime.to_policy()
        regime_observations: list[Any] = []
        selected: dict[tuple[str, int, str], str] = {}
        decision_records: list[dict[str, object]] = []
        for key, group_observations in sorted(observations_by_group.items()):
            decision = optimize_governed_pipeline(
                statistics_by_group[key],
                policy,
                model,
            )
            legal = decision.nondominated_candidate_ids
            if len(legal) != regime.expected_legal_candidate_count:
                raise ValueError(
                    f"Unexpected legal-space size for {regime.policy_id}: {key}={legal}"
                )
            if decision.selected_candidate_id is None:
                raise ValueError(f"Optimizer rejected a declared holdout group: {key}")
            if decision.selected_candidate_id not in legal:
                all_illegal.append(
                    {
                        "policy_id": regime.policy_id,
                        "scenario_id": key[0],
                        "seed": key[1],
                        "selected_candidate_id": decision.selected_candidate_id,
                        "legal_candidate_ids": list(legal),
                    }
                )
            selected[key] = decision.selected_candidate_id
            regime_observations.extend(
                item for item in group_observations if item.candidate_id in legal
            )
            decision_records.append(
                {
                    "scenario_id": key[0],
                    "seed": key[1],
                    "selected_candidate_id": decision.selected_candidate_id,
                    "legal_candidate_ids": list(legal),
                    "reason_code": decision.reason_code,
                    "performance_model_used": decision.performance_model_used,
                    "predicted_latency_ms": dict(decision.predicted_latency_ms),
                }
            )
        metrics = _selection_metrics(
            tuple(regime_observations),
            selected,
            practical_tie_fraction=model.practical_tie_fraction,
        )
        baselines = fixed_candidate_baselines(
            tuple(regime_observations),
            practical_tie_fraction=model.practical_tie_fraction,
        )
        best_fixed_id, best_fixed = min(
            baselines.items(),
            key=lambda item: (
                item[1]["mean_regret_percent"],
                item[1]["p95_regret_percent"],
                item[0],
            ),
        )
        regime_results[regime.policy_id] = {
            "policy": asdict(regime),
            "optimizer_metrics": metrics,
            "fixed_baselines": baselines,
            "best_fixed_candidate_id": best_fixed_id,
            "best_fixed_metrics": best_fixed,
            "decisions": decision_records,
        }

    primary = regime_results[config.primary_adaptive_policy_id]
    primary_metrics = primary["optimizer_metrics"]
    primary_fixed = primary["best_fixed_metrics"]
    assert isinstance(primary_metrics, dict)
    assert isinstance(primary_fixed, dict)
    strict = next(item for item in config.policy_regimes if item.policy_id == "strict")
    strict_decisions = regime_results[strict.policy_id]["decisions"]
    assert isinstance(strict_decisions, list)
    strict_correct = all(
        item["selected_candidate_id"] == POLICY_FIRST_MASKED_CHECKPOINT
        and item["reason_code"] == ONLY_LEGAL_NONDOMINATED_CANDIDATE
        and item["performance_model_used"] is False
        for item in strict_decisions
    )
    primary_decisions = primary["decisions"]
    assert isinstance(primary_decisions, list)
    fallback_count = sum(
        item["reason_code"] == OUT_OF_SUPPORT_CONSERVATIVE_FALLBACK for item in primary_decisions
    )
    fallback_rate = fallback_count / len(primary_decisions)
    gates = {
        "minimum_primary_oracle_set_hit_rate": (
            primary_metrics["oracle_set_hit_rate"] >= config.minimum_oracle_set_hit_rate
        ),
        "maximum_primary_mean_regret_percent": (
            primary_metrics["mean_regret_percent"] <= config.maximum_mean_regret_percent
        ),
        "maximum_primary_p95_regret_percent": (
            primary_metrics["p95_regret_percent"] <= config.maximum_p95_regret_percent
        ),
        "maximum_primary_regret_percent": (
            primary_metrics["maximum_regret_percent"] <= config.maximum_regret_percent
        ),
        "minimum_primary_selected_candidate_count": (
            len(primary_metrics["selected_candidate_counts"])
            >= config.minimum_selected_candidate_count
        ),
        "better_than_best_fixed_mean": (
            not config.require_better_than_best_fixed_mean
            or primary_metrics["mean_regret_percent"] < primary_fixed["mean_regret_percent"]
        ),
        "better_than_best_fixed_p95": (
            not config.require_better_than_best_fixed_p95
            or primary_metrics["p95_regret_percent"] < primary_fixed["p95_regret_percent"]
        ),
        "strict_policy_prunes_to_policy_first": strict_correct,
        "maximum_out_of_support_fallback_rate": (
            fallback_rate <= config.maximum_out_of_support_fallback_rate
        ),
        "zero_illegal_selections": not all_illegal,
        "no_systematic_material_carryover": (
            carryover_passed or not config.require_no_material_carryover
        ),
    }
    passed = all(gates.values())
    output = root / config.results_dir / run_dir.name / "evaluation.json"
    result = {
        "status": (
            "PASS_POLICY_STRATIFIED_PIPELINE_OPTIMIZER_HOLDOUT"
            if passed
            else "FAIL_POLICY_STRATIFIED_PIPELINE_OPTIMIZER_HOLDOUT_RETAIN"
        ),
        "model_frozen_before_measurement": True,
        "model_refit_or_threshold_change": False,
        "primary_adaptive_policy_id": config.primary_adaptive_policy_id,
        "measurement_run_dir": str(run_dir.relative_to(root)),
        "measurement_summary_sha256": _sha256(run_dir / "summary.json"),
        "model_sha256": config.model_sha256,
        "support_sha256": config.support_sha256,
        "primary_out_of_support_fallback_count": fallback_count,
        "primary_out_of_support_fallback_rate": fallback_rate,
        "integrity": {
            **integrity,
            "systematic_carryover_passed": carryover_passed,
        },
        "regime_results": regime_results,
        "illegal_selections": all_illegal,
        "gate_checks": gates,
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "scientific_boundary": (
            "The January/April/July/October real months were frozen after the "
            "February/May/August/November permissive-policy result was retained. "
            "Only the no-raw-join regime authorizes an adaptive performance claim."
        ),
    }
    _atomic_json(output, result)
    return output
