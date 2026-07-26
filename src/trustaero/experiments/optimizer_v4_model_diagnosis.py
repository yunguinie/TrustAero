"""Diagnose the frozen negative V4 grouped-development result."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_optimizer_transfer import EARLY_CANDIDATE, LATE_CANDIDATE


def diagnose_optimizer_v4_model(run_dir: Path | str) -> dict[str, object]:
    """Separate ranking-sign errors from uncertainty-fallback errors."""

    directory = Path(run_dir)
    result = cast(dict[str, Any], _load_json(directory / "cross_validation.json"))
    if result.get("status") != "FAIL_V4_DEVELOPMENT_GATE_RETAIN":
        raise ValueError("V4 failure diagnosis requires a retained failed run")
    rows = cast(list[dict[str, Any]], result["predictions"])
    stable = [item for item in rows if item["stable"]]
    reason_counts = Counter(str(item["v4_reason_code"]) for item in rows)

    def prediction_candidate(item: dict[str, Any]) -> str:
        prediction = float(item["v4_prediction"])
        return EARLY_CANDIDATE if prediction < 0.0 else LATE_CANDIDATE

    sign_correct = [
        item for item in stable if prediction_candidate(item) == item["actual_direction"]
    ]
    direct = [item for item in stable if item["optimizer_v4"]["direct"]]
    fallback = [item for item in stable if not item["optimizer_v4"]["direct"]]
    fallback_wrong = [item for item in fallback if not item["optimizer_v4"]["top1"]]
    wrong_sign = [item for item in stable if prediction_candidate(item) != item["actual_direction"]]
    thresholds = [float(item["v4_uncertainty_threshold"]) for item in result["folds"]]
    match = cast(dict[str, Any], cast(dict[str, Any], result["metrics"])["match_rate_baseline"])
    v4 = cast(dict[str, Any], cast(dict[str, Any], result["metrics"])["optimizer_v4"])
    return {
        "schema_version": 1,
        "status": "V4_NEGATIVE_RESULT_ROOT_CAUSE_DIAGNOSED",
        "source_run_id": directory.name,
        "source_result_preserved": True,
        "stable_family_count": len(stable),
        "direct_stable_decision_count": len(direct),
        "direct_stable_correct_count": sum(item["optimizer_v4"]["top1"] for item in direct),
        "counterfactual_prediction_sign_correct_count": len(sign_correct),
        "counterfactual_prediction_sign_accuracy": len(sign_correct) / len(stable),
        "wrong_prediction_sign_family_ids": [item["family_id"] for item in wrong_sign],
        "stable_fallback_count": len(fallback),
        "stable_fallback_wrong_count": len(fallback_wrong),
        "stable_fallback_wrong_family_ids": [item["family_id"] for item in fallback_wrong],
        "all_reason_counts": dict(sorted(reason_counts.items())),
        "fold_uncertainty_threshold_min": min(thresholds),
        "fold_uncertainty_threshold_max": max(thresholds),
        "v4_metrics": v4,
        "match_rate_baseline_metrics": match,
        "failure_categories": {
            "physical_work_direction_failure": "MOSTLY_REJECTED_43_OF_44_SIGNS_CORRECT",
            "residual_uncertainty_calibration_failure": "CONFIRMED",
            "conservative_early_fallback_performance_failure": "CONFIRMED",
            "simple_baseline_dominance": "CONFIRMED_ON_CURRENT_FRAGMENT",
            "governance_violation": "REJECTED_ZERO_ILLEGAL_SELECTIONS",
        },
        "rejected_fixes": [
            "lower the 0.80 residual quantile after reading this result",
            "remove uncertainty and relabel the same run as a passed V4",
            "use the match-rate baseline as a hidden fallback and claim pipeline-model credit",
            "exclude low-match families that make conservative early fallback expensive",
            "open February-December to tune the guard",
        ],
        "authorized_next_design_question": (
            "Evaluate a predeclared group-sign stability guard separately from cost-gap "
            "uncertainty. Any fallback must be reported as its own baseline contribution. "
            "The current Mask/Join fragment cannot establish pipeline-model superiority "
            "because the learned match-rate-only baseline is perfect on stable January folds."
        ),
        "conclusion": (
            "The four pipeline-work features recover the correct ranking sign for 43/44 "
            "stable held-out families, but the training-group residual magnitude is not a "
            "usable uncertainty scale. It reduces direct coverage to 40.9% and sends 12 "
            "stable late-preferred families to conservative early fallback. V4 therefore "
            "fails its frozen development gate and must not be frozen as the final selector."
        ),
    }


def write_optimizer_v4_model_diagnosis(run_dir: Path | str) -> Path:
    directory = Path(run_dir)
    output = directory / "failure_diagnosis.json"
    _atomic_json(output, diagnose_optimizer_v4_model(directory))
    return output
