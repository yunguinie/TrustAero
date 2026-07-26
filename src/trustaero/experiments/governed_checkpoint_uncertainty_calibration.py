"""Calibrate and audit the V3 one-sided checkpoint uncertainty guard.

Only the original EA-1 development run and its leave-one-family-out V2
predictions are used numerically.  The failed V2 holdout motivates the method
but is intentionally absent from every fitted value in this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.execution_flow_audit import _atomic_json, _git_state
from trustaero.experiments.governed_checkpoint_optimizer_holdout import (
    _confidence_oracles,
    _load_statistics_and_medians,
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


@dataclass(frozen=True, slots=True)
class CheckpointUncertaintyCalibrationConfig:
    """Frozen inputs and development-only acceptance gates for V3."""

    development_run_dir: str
    v2_calibration_path: str
    frozen_base_model_path: str
    results_dir: str
    expected_development_summary_sha256: str
    expected_development_measurements_sha256: str
    expected_v2_calibration_sha256: str
    expected_base_model_sha256: str
    coverage: float
    expected_family_count: int
    minimum_confidence_family_hit_rate: float
    maximum_mean_regret_percent: float
    maximum_regret_percent: float
    require_seed_consistency: bool
    require_clean_git: bool

    def __post_init__(self) -> None:
        if not 0.0 < self.coverage < 1.0:
            raise ValueError("Uncertainty coverage must be in (0, 1)")
        if self.expected_family_count < 2:
            raise ValueError("Uncertainty calibration needs multiple families")
        if not 0.0 <= self.minimum_confidence_family_hit_rate <= 1.0:
            raise ValueError("Uncertainty hit-rate gate must be in [0, 1]")
        if min(self.maximum_mean_regret_percent, self.maximum_regret_percent) < 0.0:
            raise ValueError("Uncertainty regret gates must be nonnegative")


def load_checkpoint_uncertainty_config(
    path: str | Path,
) -> CheckpointUncertaintyCalibrationConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return CheckpointUncertaintyCalibrationConfig(**payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def grouped_one_sided_error_bound(
    decisions: Sequence[Mapping[str, Any]],
    medians: Mapping[tuple[str, int, str], float],
    *,
    coverage: float,
) -> tuple[float, tuple[dict[str, Any], ...]]:
    """Return a finite-sample upper bound using one score per scenario family.

    The signed score is ``actual_margin - predicted_margin`` where each margin
    is ``query-first - policy-first``.  Seeds are repeated measurements of one
    scenario, so their median forms one exchangeable family-level score.
    """

    if not 0.0 < coverage < 1.0:
        raise ValueError("Grouped uncertainty coverage must be in (0, 1)")
    errors: dict[str, list[float]] = defaultdict(list)
    for row in decisions:
        scenario_id = str(row["scenario_id"])
        seed = int(row["seed"])
        estimated = cast(Mapping[str, float], row["estimated_costs_ms"])
        actual_margin = (
            medians[(scenario_id, seed, QUERY_FIRST_CHECKPOINT)]
            - medians[(scenario_id, seed, POLICY_FIRST_CHECKPOINT)]
        )
        predicted_margin = float(estimated[QUERY_FIRST_CHECKPOINT]) - float(
            estimated[POLICY_FIRST_CHECKPOINT]
        )
        # The guard only controls the risky action: switching away from the
        # conservative policy-first fallback. Residuals from cases where the
        # base model already selects policy-first do not estimate that risk and
        # would make the query-first error bound unnecessarily conservative.
        if predicted_margin < 0.0:
            errors[scenario_id].append(actual_margin - predicted_margin)
    if len(errors) < 2 or any(not values for values in errors.values()):
        raise ValueError("Grouped uncertainty scores are incomplete")
    family_rows = tuple(
        {
            "scenario_id": scenario_id,
            "seed_count": len(values),
            "median_signed_margin_error_ms": statistics.median(values),
            "minimum_signed_margin_error_ms": min(values),
            "maximum_signed_margin_error_ms": max(values),
        }
        for scenario_id, values in sorted(errors.items())
    )
    ordered = sorted(cast(float, row["median_signed_margin_error_ms"]) for row in family_rows)
    # Split-conformal finite-sample rank over complete query-action families.
    rank = min(len(ordered), math.ceil((len(ordered) + 1) * coverage))
    return max(0.0, ordered[rank - 1]), family_rows


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def calibrate_checkpoint_uncertainty_guard(
    config: CheckpointUncertaintyCalibrationConfig,
    *,
    project_root: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Calibrate V3 on original development residuals and audit it in place."""

    root = project_root.resolve()
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("V3 uncertainty calibration requires a clean Git commit")
    development_dir = root / config.development_run_dir
    v2_path = root / config.v2_calibration_path
    model_path = root / config.frozen_base_model_path
    actual_hashes = {
        "development_summary": _sha256(development_dir / "summary.json"),
        "development_measurements": _sha256(development_dir / "measurements.csv"),
        "v2_calibration": _sha256(v2_path),
        "base_model": _sha256(model_path),
    }
    expected_hashes = {
        "development_summary": config.expected_development_summary_sha256,
        "development_measurements": config.expected_development_measurements_sha256,
        "v2_calibration": config.expected_v2_calibration_sha256,
        "base_model": config.expected_base_model_sha256,
    }
    if actual_hashes != expected_hashes:
        raise ValueError("V3 uncertainty source hash mismatch")
    v2 = json.loads(v2_path.read_text(encoding="utf-8"))
    model_payload = json.loads(model_path.read_text(encoding="utf-8"))
    if v2.get("status") != "PASS_EA1_CHECKPOINT_OPTIMIZER_DEVELOPMENT":
        raise ValueError("V3 requires the passed V2 development calibration")
    if v2.get("final_model") != model_payload:
        raise ValueError("V3 base model differs from the V2 calibration")
    base_model = analytic_model_from_dict(model_payload)
    summary = json.loads((development_dir / "summary.json").read_text(encoding="utf-8"))
    oracles = _confidence_oracles(summary)
    statistics_by_key, medians = _load_statistics_and_medians(development_dir)
    decisions_v2 = cast(list[dict[str, Any]], v2["decisions"])
    bound_ms, residual_families = grouped_one_sided_error_bound(
        decisions_v2, medians, coverage=config.coverage
    )
    if len(residual_families) != config.expected_family_count:
        raise ValueError("V3 uncertainty family count changed")
    guard = CheckpointUncertaintyGuard(
        base_model=base_model,
        query_margin_error_upper_ms=bound_ms,
        coverage=config.coverage,
        calibration_family_count=len(residual_families),
    )

    decisions: list[dict[str, Any]] = []
    strict_violations = 0
    items = sorted(statistics_by_key.items())
    for index, ((scenario_id, seed), planner_statistics) in enumerate(items, start=1):
        ranking = rank_uncertainty_aware_checkpoint_candidates(
            planner_statistics, PERMISSIVE_POLICY, guard
        )
        selected = ranking.selected_candidate_id
        if selected is None:
            raise ValueError("V3 permissive audit rejected every candidate")
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
                "estimated_costs_ms": {
                    item.candidate_id: item.total_ms for item in ranking.estimates
                },
            }
        )
        strict = rank_uncertainty_aware_checkpoint_candidates(
            planner_statistics, STRICT_POLICY, guard
        )
        strict_violations += strict.selected_candidate_id != POLICY_FIRST_CHECKPOINT
        if progress_callback is not None:
            progress_callback(index, len(items), f"{scenario_id}-s{seed}")

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
    metrics = {
        "confidence_family_hit_rate": family_hit,
        "seed_consistent_family_rate": seed_consistency,
        "mean_diagnostic_median_regret_percent": statistics.mean(regrets),
        "p95_diagnostic_median_regret_percent": _p95(regrets),
        "maximum_diagnostic_median_regret_percent": max(regrets),
        "strict_policy_illegal_selection_count": strict_violations,
        "query_margin_error_upper_ms": bound_ms,
    }
    gates = {
        "minimum_confidence_family_hit_rate": (
            family_hit >= config.minimum_confidence_family_hit_rate
        ),
        "seed_consistent_selection": (
            seed_consistency == 1.0 if config.require_seed_consistency else True
        ),
        "maximum_mean_regret": (
            metrics["mean_diagnostic_median_regret_percent"] <= config.maximum_mean_regret_percent
        ),
        "maximum_regret": (
            metrics["maximum_diagnostic_median_regret_percent"] <= config.maximum_regret_percent
        ),
        "zero_illegal_selections": strict_violations == 0,
    }
    passed = all(gates.values())
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = root / config.results_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": (
            "PASS_EA1_CHECKPOINT_UNCERTAINTY_V31_DEVELOPMENT"
            if passed
            else "FAIL_EA1_CHECKPOINT_UNCERTAINTY_V31_RETAIN"
        ),
        "analysis_commit_hash": commit,
        "analysis_git_dirty": dirty,
        "source_hashes": actual_hashes,
        "numeric_calibration_source": "original EA-1 query-action development families only",
        "failed_v2_holdout_used_for_numeric_calibration": False,
        "guard": guard.to_dict(),
        "residual_families": residual_families,
        "metrics": metrics,
        "gate_checks": gates,
        "family_results": family_results,
        "decisions": decisions,
        "paper_optimizer_performance_claim_authorized": False,
        "scientific_boundary": (
            "V3 development passing only authorizes freezing the guard for a new "
            "untouched holdout; it is not independent paper performance evidence."
        ),
        "config": asdict(config),
    }
    _atomic_json(output_dir / "calibration.json", result)
    _atomic_json(output_dir / "guard.json", guard.to_dict())
    _atomic_json(root / config.results_dir / "latest_run.json", {"run_id": run_id})
    return output_dir
