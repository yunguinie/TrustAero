"""Predeclared complete-group sign-stability evaluation for V4.1."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.optimizer_v4_model_development import (
    V4ModelDevelopmentConfig,
    V4Observation,
    _candidate,
    _fit_linear,
    _fit_v4,
    _load_observations,
    _nearest_rank,
    _regret,
    load_v4_model_development_config,
)
from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_data_pilot import _git_state
from trustaero.experiments.real_optimizer_transfer import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    _load_frozen_models,
    load_real_optimizer_transfer_config,
)
from trustaero.optimizer.mask import MaskPlacementFeatures, choose_mask_placement
from trustaero.optimizer.mask_interaction import (
    choose_mask_placement_by_stable_interaction_cost,
)
from trustaero.optimizer.mask_pipeline_v4_model import PipelineV4CostModel
from trustaero.optimizer.mask_pipeline_v41 import (
    PipelineV41SignEnsemble,
    choose_mask_placement_v41,
)
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class V41Gates:
    minimum_direct_precision: float
    minimum_direct_coverage: float
    minimum_direct_early_count: int
    minimum_direct_late_count: int
    maximum_direct_regret_percent: float
    minimum_unstable_uncertainty_capture: float
    maximum_conservative_mean_regret_percent: float
    maximum_conservative_p95_regret_percent: float
    maximum_conservative_regret_percent: float


@dataclass(frozen=True, slots=True)
class V41DevelopmentConfig:
    protocol_name: str
    results_dir: str
    v4_development_config_path: str
    negative_diagnosis_path: str
    negative_diagnosis_sha256: str
    require_clean_git: bool
    gates: V41Gates
    scientific_boundary: str


def load_v41_development_config(path: Path | str) -> V41DevelopmentConfig:
    payload = _load_json(Path(path))
    return V41DevelopmentConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        v4_development_config_path=str(payload["v4_development_config_path"]),
        negative_diagnosis_path=str(payload["negative_diagnosis_path"]),
        negative_diagnosis_sha256=str(payload["negative_diagnosis_sha256"]),
        require_clean_git=bool(payload["require_clean_git"]),
        gates=V41Gates(**cast(dict[str, Any], payload["gates"])),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _zero_gap_model(
    training: list[V4Observation],
    protocol: list[V4Observation],
    config: V4ModelDevelopmentConfig,
) -> PipelineV4CostModel:
    return replace(_fit_v4(training, protocol, config), uncertainty_threshold=0.0)


def _fit_sign_ensemble(
    training: list[V4Observation],
    protocol: list[V4Observation],
    config: V4ModelDevelopmentConfig,
) -> PipelineV41SignEnsemble:
    groups = sorted({item.scenario_group for item in training})
    return PipelineV41SignEnsemble(
        primary=_zero_gap_model(training, protocol, config),
        group_deletion_models=tuple(
            _zero_gap_model(
                [item for item in training if item.scenario_group != held_group],
                protocol,
                config,
            )
            for held_group in groups
        ),
    )


def _deployed_metrics(rows: list[dict[str, Any]], scheme: str) -> dict[str, float | int]:
    stable = [item for item in rows if item["stable"]]
    regrets = [float(item[scheme]["regret_percent"]) for item in stable]
    return {
        "stable_family_count": len(stable),
        "top1_selection_rate": sum(item[scheme]["top1"] for item in stable) / len(stable),
        "within_3_percent_rate": sum(item <= 3.0 for item in regrets) / len(regrets),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _nearest_rank(regrets, 0.95),
        "max_regret_percent": max(regrets),
    }


def run_v41_grouped_evaluation(
    root: Path,
    config: V41DevelopmentConfig,
    v4_config: V4ModelDevelopmentConfig,
) -> tuple[dict[str, object], PipelineV41SignEnsemble]:
    observations = _load_observations(root, v4_config)
    transfer_config = load_real_optimizer_transfer_config(root / v4_config.v3_transfer_config_path)
    v3_primary, v3_stability = _load_frozen_models(root, transfer_config)
    groups = sorted({item.scenario_group for item in observations})
    rows: list[dict[str, Any]] = []
    folds: list[dict[str, object]] = []
    for held_group in groups:
        training = [item for item in observations if item.scenario_group != held_group]
        validation = [item for item in observations if item.scenario_group == held_group]
        ensemble = _fit_sign_ensemble(training, observations, v4_config)
        match = _fit_linear(
            training,
            lambda item: (item.stats.join_match_rate,),
            ridge_lambda=v4_config.ridge_lambda,
        )
        folds.append(
            {
                "held_out_group": held_group,
                "training_groups": sorted({item.scenario_group for item in training}),
                "ensemble_model_count": 1 + len(ensemble.group_deletion_models),
                "magnitude_threshold_used": False,
            }
        )
        for item in validation:
            decision = choose_mask_placement_v41(item.stats, ensemble)
            match_prediction = match.predict((item.stats.join_match_rate,))
            match_selected = EARLY_CANDIDATE if match_prediction < 0.0 else LATE_CANDIDATE
            direct_selected = (
                _candidate(decision.direct_placement)
                if decision.direct_placement is not None
                else None
            )
            conservative_selection = direct_selected or EARLY_CANDIDATE
            match_fallback = direct_selected or match_selected
            features = MaskPlacementFeatures(
                item.stats.join_input_rows,
                int(item.stats.sensitive_raw_width_bytes),
                item.stats.join_match_rate,
            )
            v1 = _candidate(choose_mask_placement(features).placement)
            v3 = _candidate(
                choose_mask_placement_by_stable_interaction_cost(
                    features, v3_primary, v3_stability
                ).placement
            )
            row: dict[str, Any] = {
                "family_id": item.family_id,
                "held_out_group": held_group,
                "stable": item.stable,
                "paired_ratio": item.paired_ratio,
                "actual_direction": item.actual_direction,
                "v41_direct_candidate": direct_selected,
                "v41_reason_code": decision.reason_code,
                "v41_predictions": list(decision.predictions),
                "match_rate_prediction": match_prediction,
            }
            selections = {
                "fixed_early": EARLY_CANDIDATE,
                "fixed_late": LATE_CANDIDATE,
                "match_rate_baseline": match_selected,
                "optimizer_v1": v1,
                "optimizer_v3": v3,
                "v41_conservative_early": conservative_selection,
                "v41_match_rate_fallback": match_fallback,
                "oracle": item.actual_direction,
            }
            for scheme, selected in selections.items():
                row[scheme] = {
                    "selected_candidate": selected,
                    "top1": selected == item.actual_direction,
                    "regret_percent": _regret(item.paired_ratio, selected),
                }
            rows.append(row)
    stable = [item for item in rows if item["stable"]]
    direct = [item for item in stable if item["v41_direct_candidate"] is not None]
    direct_correct = [
        item for item in direct if item["v41_direct_candidate"] == item["actual_direction"]
    ]
    direct_regrets = [
        _regret(float(item["paired_ratio"]), str(item["v41_direct_candidate"])) for item in direct
    ]
    direct_metrics = {
        "stable_direct_count": len(direct),
        "stable_direct_correct_count": len(direct_correct),
        "direct_precision": len(direct_correct) / len(direct) if direct else 0.0,
        "direct_coverage": len(direct) / len(stable) if stable else 0.0,
        "direct_early_count": sum(
            item["v41_direct_candidate"] == EARLY_CANDIDATE for item in direct
        ),
        "direct_late_count": sum(item["v41_direct_candidate"] == LATE_CANDIDATE for item in direct),
        "direct_mean_regret_percent": (statistics.mean(direct_regrets) if direct_regrets else 0.0),
        "direct_max_regret_percent": max(direct_regrets, default=0.0),
    }
    unstable = [item for item in rows if not item["stable"]]
    uncertainty_capture = (
        sum(item["v41_direct_candidate"] is None for item in unstable) / len(unstable)
        if unstable
        else 1.0
    )
    schemes = (
        "fixed_early",
        "fixed_late",
        "match_rate_baseline",
        "optimizer_v1",
        "optimizer_v3",
        "v41_conservative_early",
        "v41_match_rate_fallback",
        "oracle",
    )
    metrics = {scheme: _deployed_metrics(rows, scheme) for scheme in schemes}
    conservative_metrics = metrics["v41_conservative_early"]
    gates = {
        "minimum_direct_precision": (
            float(direct_metrics["direct_precision"]) >= config.gates.minimum_direct_precision
        ),
        "minimum_direct_coverage": (
            float(direct_metrics["direct_coverage"]) >= config.gates.minimum_direct_coverage
        ),
        "minimum_direct_early_count": (
            int(direct_metrics["direct_early_count"]) >= config.gates.minimum_direct_early_count
        ),
        "minimum_direct_late_count": (
            int(direct_metrics["direct_late_count"]) >= config.gates.minimum_direct_late_count
        ),
        "maximum_direct_regret_percent": (
            float(direct_metrics["direct_max_regret_percent"])
            <= config.gates.maximum_direct_regret_percent
        ),
        "minimum_unstable_uncertainty_capture": (
            uncertainty_capture >= config.gates.minimum_unstable_uncertainty_capture
        ),
        "maximum_conservative_mean_regret_percent": (
            float(conservative_metrics["mean_regret_percent"])
            <= config.gates.maximum_conservative_mean_regret_percent
        ),
        "maximum_conservative_p95_regret_percent": (
            float(conservative_metrics["p95_regret_percent"])
            <= config.gates.maximum_conservative_p95_regret_percent
        ),
        "maximum_conservative_regret_percent": (
            float(conservative_metrics["max_regret_percent"])
            <= config.gates.maximum_conservative_regret_percent
        ),
        "no_external_partition": True,
    }
    final_ensemble = _fit_sign_ensemble(observations, observations, v4_config)
    return (
        {
            "schema_version": 1,
            "status": (
                "PASS_V41_SIGN_STABILITY_GATE"
                if all(gates.values())
                else "FAIL_V41_SIGN_STABILITY_GATE_RETAIN"
            ),
            "outer_cross_validation": "leave_one_complete_time_window_out",
            "folds": folds,
            "direct_selector_metrics": direct_metrics,
            "deployed_metrics": metrics,
            "unstable_family_count": len(unstable),
            "unstable_uncertainty_capture": uncertainty_capture,
            "gate_checks": gates,
            "predictions": rows,
            "match_rate_baseline_superiority_claim": False,
            "pipeline_superiority_claim": False,
            "fallback_contribution_reported_separately": True,
            "external_partition_accessed": False,
            "scientific_boundary": config.scientific_boundary,
        },
        final_ensemble,
    )


def run_optimizer_v41_development(
    config: V41DevelopmentConfig,
    *,
    project_root: Path,
    config_path: Path,
) -> Path:
    root = project_root.resolve()
    if sha256_file(root / config.negative_diagnosis_path) != config.negative_diagnosis_sha256:
        raise ValueError("Frozen V4 negative diagnosis binding changed")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("V4.1 development requires a clean commit")
    v4_config = load_v4_model_development_config(root / config.v4_development_config_path)
    result, ensemble = run_v41_grouped_evaluation(root, config, v4_config)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = root / config.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(run_dir / "config.json", asdict(config))
    _atomic_json(
        run_dir / "environment.json",
        {
            "commit_hash": commit,
            "git_dirty": dirty,
            "config_sha256": sha256_file(config_path),
        },
    )
    _atomic_json(run_dir / "cross_validation.json", result)
    _atomic_json(run_dir / "pipeline_v41_ensemble.json", ensemble.to_dict())
    _atomic_json(run_dir.parent / "latest_run.json", {"run_id": run_id})
    return run_dir
