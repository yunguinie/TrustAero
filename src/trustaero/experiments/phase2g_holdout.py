"""One-shot independent holdout runner and evaluator for frozen Optimizer V3."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from trustaero.experiments.pipeline_optimizer import (
    PipelineMaskFamilyObservation,
    load_pipeline_mask_families,
)
from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures, choose_mask_placement
from trustaero.optimizer.mask_interaction import (
    InteractionMaskCostModel,
    choose_mask_placement_by_stable_interaction_cost,
)
from trustaero.reproducibility.source_freeze import audit_source_freeze, sha256_file


@dataclass(frozen=True, slots=True)
class Phase2GPreflightCheck:
    """One stable, machine-readable holdout authorization check."""

    code: str
    passed: bool
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Phase2GPreflight:
    """Read-only decision made before any holdout data are generated."""

    schema_version: int
    status: Literal["PASS", "FAIL"]
    source_commit: str | None
    checks: tuple[Phase2GPreflightCheck, ...]
    may_start_new_run: bool
    may_resume_existing_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        fields.extend(field for field in row if field not in fields)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("A percentile requires at least one value")
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _linear_percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def stratified_paired_mean_bootstrap_ci(
    differences_by_stratum: Mapping[str, Sequence[float]],
    *,
    confidence_level: float,
    repetitions: int,
    seed: int,
) -> tuple[float, float]:
    """Bootstrap a paired family-level mean while preserving row-count strata."""

    strata = {key: tuple(values) for key, values in differences_by_stratum.items() if values}
    if not strata:
        raise ValueError("Paired mean bootstrap requires at least one difference")
    if not 0.0 < confidence_level < 1.0 or repetitions < 1000:
        raise ValueError("Paired mean bootstrap settings are invalid")
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(repetitions):
        sample: list[float] = []
        for values in strata.values():
            sample.extend(values[rng.randrange(len(values))] for _ in values)
        estimates.append(statistics.mean(sample))
    alpha = (1.0 - confidence_level) / 2.0
    return _linear_percentile(estimates, alpha), _linear_percentile(estimates, 1.0 - alpha)


def _load_frozen_models(
    project_root: Path, config: Mapping[str, Any]
) -> tuple[InteractionMaskCostModel, tuple[InteractionMaskCostModel, ...]]:
    primary = InteractionMaskCostModel.from_dict(
        _read_object(project_root / str(config["primary_model_path"]))
    )
    ensemble_payload = _read_object(project_root / str(config["stability_models_path"]))
    if (
        ensemble_payload.get("model_type") != "unanimous_ridge_sign_consensus"
        or float(ensemble_payload.get("required_agreement_fraction", 0.0)) != 1.0
    ):
        raise ValueError("Frozen V3 stability-model artifact is incompatible")
    models = tuple(
        InteractionMaskCostModel.from_dict(payload)
        for payload in cast(list[dict[str, Any]], ensemble_payload["models"])
    )
    if not models:
        raise ValueError("Frozen V3 stability ensemble cannot be empty")
    return primary, models


def _development_values(project_root: Path) -> dict[str, set[int | float]]:
    observations = _read_csv(
        project_root
        / "results/phase2n_optimizer_v3_development/bounded_interaction_consensus_v3"
        / "family_observations.csv"
    )
    seeds: set[int | float] = set()
    for relative in (
        "results/phase2i_fragment_pilot/20260717T120435635390Z/raw_measurements.csv",
        "results/phase2j_fragment_boundary_confirmation/20260717T134222171980Z/"
        "raw_measurements.csv",
    ):
        seeds.update(int(row["seed"]) for row in _read_csv(project_root / relative))
    return {
        "row_counts": {int(row["join_input_rows"]) for row in observations},
        "identifier_widths": {int(row["identifier_width_bytes"]) for row in observations},
        "match_rates": {float(row["join_match_rate"]) for row in observations},
        "seeds": seeds,
    }


def audit_phase2g_preflight(
    project_root: Path,
    config_path: Path,
    authorization_path: Path,
    *,
    resume: bool,
) -> Phase2GPreflight:
    """Fail closed before creating or reading any Phase 2G result."""

    root = project_root.resolve()
    config = _read_object(config_path)
    checks: list[Phase2GPreflightCheck] = []
    source = audit_source_freeze(root, expected_environment="TrustAero_env")
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_SOURCE_FREEZE_READY",
            source.status == "READY",
            "Phase 2G requires a committed clean source tree and valid frozen hashes.",
            {"status": source.status, "source_commit": source.source_commit},
        )
    )

    authorization = _read_object(authorization_path) if authorization_path.is_file() else {}
    config_hash = sha256_file(config_path)
    authorization_ok = (
        authorization.get("status") == "authorized_to_run_once"
        and authorization.get("phase2g_authorized") is True
        and authorization.get("protocol_sha256") == config_hash
        and authorization.get("frozen_model_record") == config["frozen_model_record"]
    )
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_EXPLICIT_AUTHORIZATION",
            authorization_ok,
            "A committed authorization must bind the exact protocol and frozen V3 record.",
            {
                "authorization_path": authorization_path.relative_to(root).as_posix(),
                "authorization_status": authorization.get("status"),
                "protocol_sha256": config_hash,
            },
        )
    )

    model_record = _read_object(root / str(config["frozen_model_record"]))
    model_ok = (
        model_record.get("status") == "development_gate_passed_model_frozen"
        and model_record.get("development_gate_passes") is True
        and model_record.get("all_gate_checks_passed") is True
        and model_record.get("phase2g_authorized") is False
    )
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_FROZEN_V3_MODEL_ELIGIBLE",
            model_ok,
            "The evaluated V3 model must be frozen and must not self-authorize Phase 2G.",
            {"model_record_status": model_record.get("status")},
        )
    )

    row_counts = tuple(int(value) for value in cast(list[int], config["row_counts"]))
    widths = tuple(int(value) for value in cast(list[int], config["identifier_widths"]))
    rates = tuple(float(value) for value in cast(list[float], config["match_rates"]))
    seeds = tuple(int(value) for value in cast(list[int], config["seeds"]))
    expected_families = len(row_counts) * len(widths) * len(rates)
    expected_units = expected_families * len(seeds)
    matrix_ok = (
        config.get("one_shot_holdout") is True
        and config.get("holdout_status") == "frozen_unopened"
        and config.get("benchmarks") == ["mask_fragment"]
        and expected_families == int(config["expected_family_count"])
        and expected_units == int(config["expected_unit_count"])
        and int(config["measured_runs"]) >= 15
        and len(seeds) >= 5
    )
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_MATRIX_COMPLETE",
            matrix_ok,
            "The frozen full-factorial matrix and repetition counts must be complete.",
            {
                "family_count": expected_families,
                "unit_count": expected_units,
                "seed_count": len(seeds),
            },
        )
    )

    development = _development_values(root)
    overlaps = {
        "row_counts": sorted(set(row_counts) & development["row_counts"]),
        "identifier_widths": sorted(set(widths) & development["identifier_widths"]),
        "match_rates": sorted(set(rates) & development["match_rates"]),
        "seeds": sorted(set(seeds) & development["seeds"]),
    }
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_VALUES_UNSEEN",
            not any(overlaps.values()),
            "Every axis value and seed must be absent from V3 development data.",
            {"overlaps": overlaps},
        )
    )

    primary, stability_models = _load_frozen_models(root, config)
    in_support = all(
        primary.is_within_training_support(MaskPlacementFeatures(rows, width, rate))
        and all(
            model.is_within_training_support(MaskPlacementFeatures(rows, width, rate))
            for model in stability_models
        )
        for rows in row_counts
        for width in widths
        for rate in rates
    )
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_MAIN_MATRIX_IN_SUPPORT",
            in_support,
            "The primary holdout must test unseen interpolation, not automatic fallback.",
            {"stability_model_count": len(stability_models)},
        )
    )

    results_root = root / str(config["results_dir"])
    manifest = results_root / "one_shot_manifest.json"
    latest = results_root / "latest_run.json"
    if resume:
        one_shot_ok = manifest.is_file() and latest.is_file()
        message = "Resume requires the existing one-shot manifest and recorded run ID."
    else:
        one_shot_ok = not manifest.exists() and not latest.exists() and not results_root.exists()
        message = "A new holdout may start only when no Phase 2G artifact exists."
    checks.append(
        Phase2GPreflightCheck(
            "PHASE2G_ONE_SHOT_STATE",
            one_shot_ok,
            message,
            {
                "resume": resume,
                "results_root_exists": results_root.exists(),
                "manifest_exists": manifest.exists(),
                "latest_run_exists": latest.exists(),
            },
        )
    )

    passed = all(check.passed for check in checks)
    return Phase2GPreflight(
        schema_version=1,
        status="PASS" if passed else "FAIL",
        source_commit=source.source_commit,
        checks=tuple(checks),
        may_start_new_run=passed and not resume,
        may_resume_existing_run=passed and resume,
    )


def create_or_validate_one_shot_manifest(
    project_root: Path,
    config_path: Path,
    preflight: Phase2GPreflight,
    *,
    resume: bool,
) -> Path:
    """Consume the one-shot authorization before benchmark execution begins."""

    config = _read_object(config_path)
    results_root = project_root / str(config["results_dir"])
    manifest_path = results_root / "one_shot_manifest.json"
    payload = {
        "schema_version": 1,
        "protocol_name": config["protocol_name"],
        "protocol_sha256": sha256_file(config_path),
        "source_commit": preflight.source_commit,
        "primary_model_sha256": sha256_file(project_root / str(config["primary_model_path"])),
        "stability_models_sha256": sha256_file(project_root / str(config["stability_models_path"])),
    }
    if resume:
        existing = _read_object(manifest_path)
        for key, value in payload.items():
            if existing.get(key) != value:
                raise ValueError(f"Phase 2G resume manifest differs at {key}")
        return manifest_path
    if results_root.exists():
        raise ValueError("Phase 2G result root already exists; refusing a second holdout")
    payload["consumed_at"] = datetime.now(UTC).isoformat()
    payload["labels_opened"] = False
    _write_json(manifest_path, payload)
    return manifest_path


def _regret(item: PipelineMaskFamilyObservation, placement: MaskPlacement) -> float:
    actual = item.observed_log_early_late_ratio
    ratio = (
        max(1.0, math.exp(actual))
        if placement is MaskPlacement.EARLY
        else max(1.0, math.exp(-actual))
    )
    return (ratio - 1.0) * 100.0


def _prediction_row(
    item: PipelineMaskFamilyObservation,
    *,
    scheme: str,
    placement: MaskPlacement,
    direct: bool,
    reason: str,
    predicted: float | None,
) -> dict[str, Any]:
    oracle = MaskPlacement.EARLY if item.observed_log_early_late_ratio < 0.0 else MaskPlacement.LATE
    regret = _regret(item, placement)
    return {
        "evaluation_scheme": scheme,
        "family_id": item.family_id,
        "seed_count": item.seed_count,
        "join_input_rows": item.features.join_input_rows,
        "identifier_width_bytes": item.features.identifier_width_bytes,
        "join_match_rate": item.features.join_match_rate,
        "observed_log_early_late_ratio": item.observed_log_early_late_ratio,
        "predicted_log_early_late_ratio": predicted,
        "selected_placement": placement.value,
        "oracle_placement": oracle.value,
        "exact_top1": placement is oracle,
        "within_tie_threshold": regret <= item.tie_threshold_fraction * 100.0,
        "regret_percent": regret,
        "direct_model_decision": direct,
        "reason_code": reason,
    }


def _scheme_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    regrets = [float(row["regret_percent"]) for row in rows]
    direct = [row for row in rows if bool(row["direct_model_decision"])]
    return {
        "family_count": len(rows),
        "exact_top1_rate": sum(bool(row["exact_top1"]) for row in rows) / len(rows),
        "within_tie_rate": sum(bool(row["within_tie_threshold"]) for row in rows) / len(rows),
        "mean_regret_percent": statistics.mean(regrets),
        "p95_regret_percent": _percentile(regrets, 0.95),
        "max_regret_percent": max(regrets),
        "direct_model_coverage": len(direct) / len(rows),
        "direct_early_decision_count": sum(
            row["selected_placement"] == MaskPlacement.EARLY.value for row in direct
        ),
        "direct_late_decision_count": sum(
            row["selected_placement"] == MaskPlacement.LATE.value for row in direct
        ),
    }


def _governance_audit(
    primary: InteractionMaskCostModel,
    stability_models: tuple[InteractionMaskCostModel, ...],
) -> dict[str, bool]:
    exposure = choose_mask_placement_by_stable_interaction_cost(
        MaskPlacementFeatures(125_000, 384, 0.7, max_raw_exposure_rows=0),
        primary,
        stability_models,
    )
    fail_closed = False
    try:
        choose_mask_placement_by_stable_interaction_cost(
            MaskPlacementFeatures(
                125_000,
                384,
                0.7,
                early_mask_legal=False,
                late_mask_legal=False,
            ),
            primary,
            stability_models,
        )
    except ValueError:
        fail_closed = True
    return {
        "raw_exposure_limit_forces_early": exposure.placement is MaskPlacement.EARLY,
        "governance_forced_decision_is_not_model_decision": (not exposure.direct_model_decision),
        "no_legal_candidate_fails_closed": fail_closed,
    }


def evaluate_phase2g_holdout(
    project_root: Path,
    config_path: Path,
    run_dir: Path,
) -> Path:
    """Open holdout labels once, after the complete benchmark run exists."""

    config = _read_object(config_path)
    run_summary = _read_object(run_dir / "summary.json")
    expected_units = int(config["expected_unit_count"])
    if not (
        run_summary.get("status") == "complete"
        and int(run_summary.get("unit_count", -1)) == expected_units
        and run_summary.get("all_validations_passed") is True
        and int(run_summary.get("result_equivalent_fragment_count", -1)) == expected_units
        and int(run_summary.get("distinct_physical_plan_fragment_count", -1)) == expected_units
        and int(run_summary.get("spilled_unit_count", -1)) == 0
    ):
        raise ValueError("Phase 2G benchmark is incomplete or failed its physical checks")

    primary, stability_models = _load_frozen_models(project_root, config)
    families = load_pipeline_mask_families(
        [run_dir], tie_threshold_fraction=float(config["tie_threshold_fraction"])
    )
    if len(families) != int(config["expected_family_count"]):
        raise ValueError("Phase 2G family count differs from the frozen protocol")

    rows: list[dict[str, Any]] = []
    paired: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in families:
        v3 = choose_mask_placement_by_stable_interaction_cost(
            item.features, primary, stability_models
        )
        v1 = choose_mask_placement(item.features)
        oracle = (
            MaskPlacement.EARLY if item.observed_log_early_late_ratio < 0.0 else MaskPlacement.LATE
        )
        placements = (
            (
                "optimizer_v3_frozen",
                v3.placement,
                v3.direct_model_decision,
                v3.reason_code,
                v3.predicted_log_early_late_ratio,
            ),
            ("optimizer_v1_frozen", v1.placement, False, v1.reason_code, None),
            ("fixed_early", MaskPlacement.EARLY, False, "", None),
            ("fixed_late", MaskPlacement.LATE, False, "", None),
            ("oracle_experimental_upper_bound", oracle, False, "", None),
        )
        for scheme, placement, direct, reason, predicted in placements:
            row = _prediction_row(
                item,
                scheme=scheme,
                placement=placement,
                direct=direct,
                reason=reason,
                predicted=predicted,
            )
            rows.append(row)
            paired[item.family_id][scheme] = row

    by_scheme: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scheme[str(row["evaluation_scheme"])].append(row)
    schemes = {name: _scheme_summary(values) for name, values in by_scheme.items()}
    v3_metrics = schemes["optimizer_v3_frozen"]
    v1_metrics = schemes["optimizer_v1_frozen"]

    within_differences: dict[str, list[float]] = defaultdict(list)
    regret_differences: dict[str, list[float]] = defaultdict(list)
    for family_rows in paired.values():
        v3_row = family_rows["optimizer_v3_frozen"]
        v1_row = family_rows["optimizer_v1_frozen"]
        stratum = str(v3_row["join_input_rows"])
        within_differences[stratum].append(
            float(bool(v3_row["within_tie_threshold"]))
            - float(bool(v1_row["within_tie_threshold"]))
        )
        regret_differences[stratum].append(
            float(v3_row["regret_percent"]) - float(v1_row["regret_percent"])
        )
    bootstrap = cast(dict[str, Any], config["paired_bootstrap"])
    confidence = float(bootstrap["confidence_level"])
    repetitions = int(bootstrap["repetitions"])
    seed = int(bootstrap["seed"])
    within_ci = stratified_paired_mean_bootstrap_ci(
        within_differences,
        confidence_level=confidence,
        repetitions=repetitions,
        seed=seed,
    )
    regret_ci = stratified_paired_mean_bootstrap_ci(
        regret_differences,
        confidence_level=confidence,
        repetitions=repetitions,
        seed=seed + 1,
    )
    governance = _governance_audit(primary, stability_models)
    checks = {
        "all_validations_pass": run_summary["all_validations_passed"] is True,
        "all_family_results_equivalent": (
            int(run_summary["result_equivalent_fragment_count"]) == expected_units
        ),
        "all_family_physical_plans_distinct": (
            int(run_summary["distinct_physical_plan_fragment_count"]) == expected_units
        ),
        "no_spill": int(run_summary["spilled_unit_count"]) == 0,
        "within_tie_rate_strictly_improves_v1": (
            v3_metrics["within_tie_rate"] > v1_metrics["within_tie_rate"]
        ),
        "mean_regret_does_not_worsen_v1": (
            v3_metrics["mean_regret_percent"] <= v1_metrics["mean_regret_percent"]
        ),
        "p95_regret_does_not_worsen_v1": (
            v3_metrics["p95_regret_percent"] <= v1_metrics["p95_regret_percent"]
        ),
        "max_regret_does_not_worsen_v1": (
            v3_metrics["max_regret_percent"] <= v1_metrics["max_regret_percent"]
        ),
        "minimum_direct_coverage": (
            v3_metrics["direct_model_coverage"] >= float(config["minimum_direct_coverage"])
        ),
        "direct_decisions_include_both_placements": (
            v3_metrics["direct_early_decision_count"] > 0
            and v3_metrics["direct_late_decision_count"] > 0
        ),
        "all_governance_audits_pass": all(governance.values()),
    }
    holdout_gate_passes = all(checks.values())
    claims = {
        "within_3_percent_improvement": {
            "point_difference": (v3_metrics["within_tie_rate"] - v1_metrics["within_tie_rate"]),
            "paired_confidence_interval": list(within_ci),
            "authorized": within_ci[0] > 0.0,
        },
        "mean_regret_reduction": {
            "point_difference_percent": (
                v3_metrics["mean_regret_percent"] - v1_metrics["mean_regret_percent"]
            ),
            "paired_confidence_interval_percent": list(regret_ci),
            "authorized": regret_ci[1] < 0.0,
        },
    }
    output_dir = run_dir / "optimizer_v3_holdout_evaluation"
    summary = {
        "evaluation_scope": "phase2g_independent_optimizer_v3_holdout",
        "status": "complete_holdout_consumed",
        "run_id": run_summary["run_id"],
        "family_count": len(families),
        "unit_count": expected_units,
        "schemes": schemes,
        "paired_claims": claims,
        "governance_audit": governance,
        "holdout_gate": {"passes": holdout_gate_passes, "checks": checks},
        "scientific_boundary": (
            "This one-shot Phase 2G result is consumed regardless of outcome. It cannot "
            "be reused as an independent holdout for a modified optimizer."
        ),
    }
    _write_csv(output_dir / "family_predictions.csv", rows)
    _write_json(output_dir / "summary.json", summary)
    _write_json(
        output_dir / "paired_differences.json",
        {
            "within_3_percent": within_differences,
            "regret_percent": regret_differences,
        },
    )
    manifest_path = project_root / str(config["results_dir"]) / "one_shot_manifest.json"
    manifest = _read_object(manifest_path)
    manifest["labels_opened"] = True
    manifest["labels_opened_at"] = datetime.now(UTC).isoformat()
    manifest["run_id"] = run_summary["run_id"]
    manifest["holdout_gate_passes"] = holdout_gate_passes
    _write_json(manifest_path, manifest)
    return output_dir
