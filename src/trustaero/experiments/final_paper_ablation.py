"""Build paper-ready ablation tables only from already frozen evidence.

This analysis never reruns DuckDB, refits a model, or changes a decision
threshold.  A component deletion sets one family of frozen cost coefficients
to zero and evaluates the unchanged selector on the original independent
holdout.  This makes the ablation reproducible without contaminating the
holdout measurements.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trustaero.experiments.execution_aware_calibration import (
    NonnegativeAnalyticFit,
    evaluate_fit_selections,
)
from trustaero.experiments.execution_flow_audit import _atomic_json
from trustaero.experiments.governed_pipeline_cost_calibration import (
    _selection_metrics,
    fixed_candidate_baselines,
)
from trustaero.experiments.governed_pipeline_cost_holdout import (
    _load_holdout_observations,
    _select_with_frozen_model,
)
from trustaero.experiments.lineage_checkpoint_calibration import (
    STABLE_PREFERENCES,
    evaluate_fixed_baselines,
    load_lineage_calibration_observations,
)
from trustaero.experiments.lineage_checkpoint_holdout import (
    _model as load_lineage_model,
)
from trustaero.experiments.lineage_checkpoint_holdout import (
    _selection_metrics as lineage_selection_metrics,
)


@dataclass(frozen=True)
class FinalPaperAblationConfig:
    """Paths to immutable inputs used by the final paper ablation."""

    phase0_cases: str
    phase0_evaluation: str
    system_summary: str
    system_evaluation: str
    multisource_summary: str
    pipeline_holdout_run: str
    pipeline_model: str
    lineage_holdout_run: str
    lineage_model: str
    results_dir: str


def load_config(path: Path) -> FinalPaperAblationConfig:
    """Load the small, explicit evidence manifest."""

    return FinalPaperAblationConfig(**json.loads(path.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric_view(metrics: dict[str, Any]) -> dict[str, Any]:
    """Keep only aggregate metrics suitable for a paper table."""

    return {
        key: metrics[key]
        for key in (
            "decision_count",
            "oracle_set_hit_rate",
            "mean_regret_percent",
            "p95_regret_percent",
            "maximum_regret_percent",
        )
        if key in metrics
    }


def _without_prefixes(
    coefficients: dict[str, float],
    prefixes: tuple[str, ...],
) -> dict[str, float]:
    """Delete one physical-cost family without fitting replacement weights."""

    return {
        name: (0.0 if name.startswith(prefixes) else value) for name, value in coefficients.items()
    }


def _validator_and_certificate_tables(
    root: Path,
    config: FinalPaperAblationConfig,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    with (root / config.phase0_cases).open(encoding="utf-8-sig", newline="") as handle:
        cases = list(csv.DictReader(handle))
    phase0 = json.loads((root / config.phase0_evaluation).read_text(encoding="utf-8"))
    multisource = json.loads((root / config.multisource_summary).read_text(encoding="utf-8"))

    outcome_ids = {
        status: [row["case_id"] for row in cases if row["actual_status"] == status]
        for status in ("ACCEPT", "REWRITE", "CLARIFY", "REJECT")
    }
    validator = {
        "interpretation": "four_handling_outcomes_not_disabled_safety_layers",
        "outcome_case_ids": outcome_ids,
        "status_accuracy": phase0["metrics"]["status_accuracy"],
        "reason_code_accuracy": phase0["metrics"]["reason_code_accuracy"],
        "false_reject_rate": phase0["metrics"]["false_reject_rate"],
        "violation_detection_rate": phase0["metrics"]["detection_rate"],
    }

    certificate_cases = [
        {
            "source": "phase0",
            "case_id": row["case_id"],
            "category": row["case_category"],
            "status": row["actual_status"],
            "reason_codes": row["actual_reason_codes"].split("|")
            if row["actual_reason_codes"]
            else [],
        }
        for row in cases
        if row["case_id"] >= "P0-012"
    ]
    end_to_end_faults = [
        {
            "source": "multisource_v2",
            "case_id": item["fault_id"],
            "category": "end_to_end_tamper",
            "status": item["status"],
            "reason_codes": item["actual_reason_codes"],
        }
        for item in multisource["fault_injection"]
    ]
    certificate = {
        "phase0_certificate_cases": len(certificate_cases),
        "end_to_end_tamper_cases": len(end_to_end_faults),
        "valid_certificate_status": multisource["certificate"]["status"],
        "claim_boundary": multisource["claim_boundary"],
        "all_injected_faults_rejected": all(
            item["status"] == "REJECT" for item in end_to_end_faults
        ),
    }
    return validator, certificate, certificate_cases + end_to_end_faults


def _system_layers(
    root: Path,
    config: FinalPaperAblationConfig,
) -> list[dict[str, Any]]:
    summary = json.loads((root / config.system_summary).read_text(encoding="utf-8"))
    evaluation = json.loads((root / config.system_evaluation).read_text(encoding="utf-8"))
    if not evaluation["source_lineage_performance_evidence_authorized"]:
        raise ValueError("System ablation evidence is not authorized")
    return list(summary["layer_summaries"])


def _pipeline_ablation(
    root: Path,
    config: FinalPaperAblationConfig,
) -> dict[str, Any]:
    observations, legal, integrity = _load_holdout_observations(root / config.pipeline_holdout_run)
    model = json.loads((root / config.pipeline_model).read_text(encoding="utf-8"))
    coefficient_groups = {
        "without_mask_and_policy_work": ("mask.", "policy_hash."),
        "without_join_work": ("join.",),
        "without_checkpoint_work": ("checkpoint.",),
        "without_lineage_work": ("lineage.",),
        "without_pipeline_breaker_work": ("pipeline_breaker.",),
    }
    variants: dict[str, Any] = {}
    for name, prefixes in {"full_frozen_model": (), **coefficient_groups}.items():
        variant = dict(model)
        coefficients = {str(key): float(value) for key, value in model["coefficients"].items()}
        if prefixes:
            coefficients = _without_prefixes(coefficients, prefixes)
        variant["coefficients"] = coefficients
        selected, _ = _select_with_frozen_model(observations, variant)
        illegal = [key for key, candidate in selected.items() if candidate not in legal[key]]
        metrics = _selection_metrics(
            observations,
            selected,
            practical_tie_fraction=float(model["practical_tie_fraction"]),
        )
        variants[name] = {
            **_metric_view(metrics),
            "selected_candidate_counts": metrics["selected_candidate_counts"],
            "illegal_selection_count": len(illegal),
        }
    baselines = fixed_candidate_baselines(
        observations,
        practical_tie_fraction=float(model["practical_tie_fraction"]),
    )
    return {
        "method": "zero_frozen_coefficient_family_without_refit",
        "measurement_integrity": integrity,
        "variants": variants,
        "fixed_baselines": {name: _metric_view(value) for name, value in baselines.items()},
    }


def _lineage_ablation(
    root: Path,
    config: FinalPaperAblationConfig,
) -> dict[str, Any]:
    run = root / config.lineage_holdout_run
    observations = load_lineage_calibration_observations(
        run,
        allow_complete_admission_negative=True,
    )
    fit, frozen = load_lineage_model(root / config.lineage_model)
    tie = float(frozen["model"]["practical_tie_fraction"])
    coefficient_groups = {
        "without_checkpoint_work": ("checkpoint.",),
        "without_lineage_work": ("lineage.",),
        "without_source_scan_work": ("source.",),
        "without_pipeline_breaker_work": ("pipeline_breaker.",),
    }
    variants: dict[str, Any] = {}
    for name, prefixes in {"full_frozen_model": (), **coefficient_groups}.items():
        coefficients = dict(fit.coefficients)
        if prefixes:
            coefficients = _without_prefixes(coefficients, prefixes)
        variant = NonnegativeAnalyticFit(
            intercept_ms=fit.intercept_ms,
            coefficients=tuple(sorted(coefficients.items())),
            ridge_lambda=fit.ridge_lambda,
            iterations=0,
            converged=True,
        )
        decisions = evaluate_fit_selections(
            variant,
            observations,
            stable_preferences=STABLE_PREFERENCES,
            practical_tie_fraction=tie,
        )
        variants[name] = _metric_view(lineage_selection_metrics(decisions))
    baselines = evaluate_fixed_baselines(
        observations,
        practical_tie_fraction=tie,
    )
    return {
        "method": "zero_frozen_coefficient_family_without_refit",
        "variants": variants,
        "fixed_baselines": {name: _metric_view(value) for name, value in baselines.items()},
    }


def _write_report(output: Path, payload: dict[str, Any]) -> None:
    validator = payload["validator"]
    pipeline = payload["optimizer_ablations"]["governed_pipeline"]["variants"]
    lineage = payload["optimizer_ablations"]["lineage_checkpoint"]["variants"]
    lines = [
        "# Final paper ablation and integrity tables",
        "",
        "This report is derived only from frozen measurements and models. No model was",
        "refit and no holdout threshold was changed.",
        "",
        "## Validator outcomes",
        "",
        "| Outcome | Case IDs |",
        "|---|---|",
    ]
    for status, ids in validator["outcome_case_ids"].items():
        lines.append(f"| {status} | {', '.join(ids)} |")
    lines += [
        "",
        "The four rows are handling outcomes, not unsafe layer-disable ablations.",
        "",
        "## Governed-pipeline optimizer component deletion",
        "",
        "| Variant | Oracle-set hit | Mean regret | P95 regret | Max regret |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metrics in pipeline.items():
        lines.append(
            f"| {name} | {metrics['oracle_set_hit_rate']:.3f} | "
            f"{metrics['mean_regret_percent']:.3f}% | "
            f"{metrics['p95_regret_percent']:.3f}% | "
            f"{metrics['maximum_regret_percent']:.3f}% |"
        )
    lines += [
        "",
        "## Lineage-checkpoint optimizer component deletion",
        "",
        "| Variant | Oracle-set hit | Mean regret | P95 regret | Max regret |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metrics in lineage.items():
        lines.append(
            f"| {name} | {metrics['oracle_set_hit_rate']:.3f} | "
            f"{metrics['mean_regret_percent']:.3f}% | "
            f"{metrics['p95_regret_percent']:.3f}% | "
            f"{metrics['maximum_regret_percent']:.3f}% |"
        )
    lines += [
        "",
        "## Certificate fault matrix",
        "",
        "| Source | Case | Category | Status | Reason codes |",
        "|---|---|---|---|---|",
    ]
    for item in payload["certificate_fault_matrix"]:
        lines.append(
            f"| {item['source']} | {item['case_id']} | {item['category']} | "
            f"{item['status']} | {', '.join(item['reason_codes']) or 'none'} |"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_final_paper_ablation(
    project_root: Path,
    config_path: Path,
) -> Path:
    """Generate one immutable, paper-facing ablation bundle."""

    root = project_root.resolve()
    config_file = config_path.resolve()
    config = load_config(config_file)
    validator, certificate, fault_matrix = _validator_and_certificate_tables(root, config)
    output = root / config.results_dir
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "analysis_type": "offline_frozen_evidence_ablation",
        "holdout_refit_performed": False,
        "threshold_retuning_performed": False,
        "config_path": str(config_file.relative_to(root)),
        "config_sha256": _sha256(config_file),
        "validator": validator,
        "system_four_layer_ablation": _system_layers(root, config),
        "certificate": certificate,
        "certificate_fault_matrix": fault_matrix,
        "optimizer_ablations": {
            "governed_pipeline": _pipeline_ablation(root, config),
            "lineage_checkpoint": _lineage_ablation(root, config),
        },
    }
    _atomic_json(output / "ablation.json", payload)
    _write_report(output / "report.md", payload)
    return output
