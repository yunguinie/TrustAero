"""Fail-closed readiness audit for developing the next Mask optimizer.

The audit deliberately authorizes only *designing* Optimizer V3.  It does not
promote a rejected model, authorize Phase 2G, or claim that a new optimizer is
already accurate.  Every gate is derived from frozen development evidence so
that a future model cannot silently change its training boundary.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from trustaero.experiments.paired_claims import stratified_paired_bootstrap_ci
from trustaero.experiments.pipeline_optimizer import load_pipeline_mask_families
from trustaero.reproducibility.source_freeze import audit_source_freeze, sha256_file


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One stable, machine-readable V3 readiness gate."""

    code: str
    passed: bool
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PositionEffectResult:
    """Aggregate candidate-position effect for one source run and component."""

    run_id: str
    component: str
    unit_count: int
    median_position_1_over_0: float
    confidence_interval_95: tuple[float, float]
    tolerance_interval: tuple[float, float]
    passed: bool


@dataclass(frozen=True, slots=True)
class OptimizerV3ReadinessAudit:
    """Complete decision and evidence for the Optimizer V3 development gate."""

    schema_version: int
    status: Literal["PASS", "FAIL"]
    source_commit: str | None
    checks: tuple[ReadinessCheck, ...]
    position_effects: tuple[PositionEffectResult, ...]
    optimizer_v3_protocol_design_authorized: bool
    optimizer_v3_training_authorized: bool
    phase2g_authorized: bool
    scientific_boundary: str

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-safe representation."""

        return asdict(self)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _inside_root(project_root: Path, relative_path: str) -> Path:
    """Resolve one configured artifact while rejecting traversal."""

    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(f"Audit paths must be relative: {relative_path}")
    root = project_root.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Audit path escapes the project root: {relative_path}")
    return resolved


def _stable_seed(seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def analyze_position_effects(
    raw_measurement_path: Path,
    *,
    tolerance_fraction: float,
    confidence_level: float,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> tuple[tuple[PositionEffectResult, ...], bool, dict[str, Any]]:
    """Check balance and systematic position effects in two-candidate runs.

    Each unit/component is observed in both execution positions.  We collapse
    repetitions to a unit median, then bootstrap the median position-1 /\
    position-0 ratio by complete workload family.  Large individual noise is
    therefore not confused with a systematic order bias.
    """

    rows = _read_csv(raw_measurement_path)
    grouped: dict[tuple[str, str], dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    family_by_unit: dict[tuple[str, str], str] = {}
    run_ids: set[str] = set()
    for row in rows:
        key = (row["unit_id"], row["component"])
        position = int(row["order_position"])
        grouped[key][position].append(float(row["latency_ms"]))
        family_by_unit[key] = f"n{row['row_count']}-w{row['identifier_width']}-m{row['match_rate']}"
        run_ids.add(row["run_id"])

    balance_failures: list[str] = []
    ratios: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for key, positions in grouped.items():
        if set(positions) != {0, 1}:
            balance_failures.append(f"{key[0]}:{key[1]}:positions={sorted(positions)}")
            continue
        counts = (len(positions[0]), len(positions[1]))
        if abs(counts[0] - counts[1]) > 1:
            balance_failures.append(f"{key[0]}:{key[1]}:counts={counts}")
            continue
        position_0 = statistics.median(positions[0])
        position_1 = statistics.median(positions[1])
        if position_0 <= 0.0 or position_1 <= 0.0:
            balance_failures.append(f"{key[0]}:{key[1]}:non_positive_latency")
            continue
        ratios[key[1]][family_by_unit[key]].append(position_1 / position_0)

    run_id = next(iter(run_ids)) if len(run_ids) == 1 else "MULTIPLE_OR_MISSING"
    lower_limit = 1.0 - tolerance_fraction
    upper_limit = 1.0 + tolerance_fraction
    effects: list[PositionEffectResult] = []
    for component, strata in sorted(ratios.items()):
        values = [value for family_values in strata.values() for value in family_values]
        lower, upper = stratified_paired_bootstrap_ci(
            strata,
            confidence_level=confidence_level,
            repetitions=bootstrap_repetitions,
            seed=_stable_seed(bootstrap_seed, f"{run_id}:{component}"),
        )
        effects.append(
            PositionEffectResult(
                run_id=run_id,
                component=component,
                unit_count=len(values),
                median_position_1_over_0=statistics.median(values),
                confidence_interval_95=(lower, upper),
                tolerance_interval=(lower_limit, upper_limit),
                passed=lower >= lower_limit and upper <= upper_limit,
            )
        )
    balanced = not balance_failures and bool(grouped) and len(run_ids) == 1
    return (
        tuple(effects),
        balanced,
        {
            "row_count": len(rows),
            "unit_component_count": len(grouped),
            "run_ids": sorted(run_ids),
            "balance_failures": balance_failures,
        },
    )


def _record_matches(payload: Mapping[str, Any], requirements: Mapping[str, Any]) -> bool:
    """Match the small scalar/list boundary declared in the audit config."""

    return all(payload.get(key) == expected for key, expected in requirements.items())


def audit_optimizer_v3_readiness(
    project_root: Path,
    config_path: Path,
) -> OptimizerV3ReadinessAudit:
    """Run every predeclared gate and fail closed on missing evidence."""

    root = project_root.resolve()
    config = _read_object(config_path)
    checks: list[ReadinessCheck] = []

    source_audit = audit_source_freeze(
        root,
        expected_environment=str(config["expected_python_environment"]),
    )
    checks.append(
        ReadinessCheck(
            "V3_SOURCE_FREEZE_READY",
            source_audit.status == "READY",
            "V3 design requires a committed, clean source snapshot and valid frozen hashes.",
            {
                "status": source_audit.status,
                "source_commit": source_audit.source_commit,
                "diagnostic_codes": [item.code for item in source_audit.diagnostics],
            },
        )
    )

    artifact_results: list[dict[str, Any]] = []
    for entry in cast(list[dict[str, Any]], config["required_artifacts"]):
        path = _inside_root(root, str(entry["path"]))
        expected = str(entry["sha256"])
        actual = sha256_file(path) if path.is_file() else None
        artifact_results.append(
            {
                "path": str(entry["path"]),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "passed": actual == expected,
            }
        )
    checks.append(
        ReadinessCheck(
            "V3_REQUIRED_ARTIFACTS_IMMUTABLE",
            all(item["passed"] for item in artifact_results),
            "Every development input used by the audit must match its frozen SHA-256.",
            {"artifacts": artifact_results},
        )
    )

    record_results: list[dict[str, Any]] = []
    for entry in cast(list[dict[str, Any]], config["required_frozen_records"]):
        path = _inside_root(root, str(entry["path"]))
        payload = _read_object(path) if path.is_file() else {}
        requirements = cast(dict[str, Any], entry["required_fields"])
        passed = bool(payload) and _record_matches(payload, requirements)
        record_results.append(
            {
                "path": str(entry["path"]),
                "passed": passed,
                "required_fields": requirements,
            }
        )
    checks.append(
        ReadinessCheck(
            "V3_NEGATIVE_AND_BOUNDARY_RECORDS_PRESERVED",
            all(item["passed"] for item in record_results),
            "V3 must consume, not overwrite, the frozen Phase 2J-2M decisions.",
            {"records": record_results},
        )
    )

    phase2g_paths = [
        _inside_root(root, str(path)) for path in cast(list[str], config["forbidden_phase2g_paths"])
    ]
    existing_phase2g = [
        path.relative_to(root).as_posix() for path in phase2g_paths if path.exists()
    ]
    checks.append(
        ReadinessCheck(
            "V3_PHASE2G_REMAINS_UNTOUCHED",
            not existing_phase2g,
            "The independent holdout must not exist before a V3 protocol and model are frozen.",
            {"existing_paths": existing_phase2g},
        )
    )

    run_dirs = [
        _inside_root(root, str(path)) for path in cast(list[str], config["source_run_dirs"])
    ]
    families = load_pipeline_mask_families(
        cast(list[str | Path], run_dirs),
        tie_threshold_fraction=float(config["tie_threshold_fraction"]),
    )
    family_ids = [family.family_id for family in families]
    minimum_replicates = int(config["minimum_replicates_per_family"])
    family_gate = (
        len(families) == int(config["expected_family_count"])
        and len(family_ids) == len(set(family_ids))
        and all(family.seed_count >= minimum_replicates for family in families)
    )
    checks.append(
        ReadinessCheck(
            "V3_COMPLETE_FAMILY_GROUPING",
            family_gate,
            "Seeds stay inside complete rows-width-match families; no replicate is split.",
            {
                "family_count": len(families),
                "unique_family_count": len(set(family_ids)),
                "minimum_observed_replicates": min(family.seed_count for family in families),
                "required_minimum_replicates": minimum_replicates,
                "cross_validation": config["cross_validation"],
            },
        )
    )

    allowed_features = tuple(str(value) for value in cast(list[str], config["allowed_inputs"]))
    forbidden_tokens = tuple(
        str(value).casefold() for value in cast(list[str], config["forbidden_input_tokens"])
    )
    contaminated = [
        name
        for name in allowed_features
        if any(token in name.casefold() for token in forbidden_tokens)
    ]
    cross_validation_ok = config["cross_validation"] == "leave_one_complete_family_out"
    checks.append(
        ReadinessCheck(
            "V3_PRE_EXECUTION_INPUTS_ONLY",
            not contaminated and cross_validation_ok,
            "Only statistics available before candidate execution may enter V3.",
            {
                "allowed_inputs": list(allowed_features),
                "contaminated_inputs": contaminated,
                "cross_validation": config["cross_validation"],
            },
        )
    )

    all_effects: list[PositionEffectResult] = []
    all_balanced = True
    balance_evidence: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        effects, balanced, evidence = analyze_position_effects(
            run_dir / "raw_measurements.csv",
            tolerance_fraction=float(config["position_effect_tolerance_fraction"]),
            confidence_level=float(config["confidence_level"]),
            bootstrap_repetitions=int(config["bootstrap_repetitions"]),
            bootstrap_seed=int(config["bootstrap_seed"]),
        )
        all_effects.extend(effects)
        all_balanced = all_balanced and balanced
        evidence["run_dir"] = run_dir.relative_to(root).as_posix()
        balance_evidence.append(evidence)
    checks.append(
        ReadinessCheck(
            "V3_EXECUTION_POSITIONS_BALANCED",
            all_balanced,
            "Every candidate is observed in both positions with counts differing by at most one.",
            {"runs": balance_evidence},
        )
    )
    position_gate = bool(all_effects) and all(effect.passed for effect in all_effects)
    checks.append(
        ReadinessCheck(
            "V3_NO_MATERIAL_SYSTEMATIC_POSITION_EFFECT",
            position_gate,
            "Each aggregate position-effect CI must remain inside the predeclared tolerance.",
            {"effect_count": len(all_effects)},
        )
    )

    phase2k_summary = _read_object(_inside_root(root, str(config["phase2k_summary_path"])))
    governance = cast(dict[str, Any], phase2k_summary.get("governance_legality_audit", {}))
    governance_gate = bool(governance) and all(value is True for value in governance.values())
    rejected_gate = (
        phase2k_summary.get("status") == "development_only_rejected_by_predeclared_gate"
        and cast(dict[str, Any], phase2k_summary.get("development_gate", {})).get("passes") is False
        and phase2k_summary.get("phase2g_authorized") is False
    )
    checks.extend(
        (
            ReadinessCheck(
                "V3_GOVERNANCE_PRECEDES_COST",
                governance_gate,
                "Illegal or over-exposing candidates must be removed before cost ranking.",
                {"governance_legality_audit": governance},
            ),
            ReadinessCheck(
                "V3_REJECTED_V2_NOT_PROMOTED",
                rejected_gate,
                "The failed V2 model remains development-only and cannot seed a paper claim.",
                {
                    "status": phase2k_summary.get("status"),
                    "development_gate_passes": cast(
                        dict[str, Any], phase2k_summary.get("development_gate", {})
                    ).get("passes"),
                    "phase2g_authorized": phase2k_summary.get("phase2g_authorized"),
                },
            ),
        )
    )

    passed = all(check.passed for check in checks)
    return OptimizerV3ReadinessAudit(
        schema_version=1,
        status="PASS" if passed else "FAIL",
        source_commit=source_audit.source_commit,
        checks=tuple(checks),
        position_effects=tuple(all_effects),
        optimizer_v3_protocol_design_authorized=passed,
        optimizer_v3_training_authorized=False,
        phase2g_authorized=False,
        scientific_boundary=(
            "PASS authorizes writing and pre-registering an Optimizer V3 development protocol "
            "only. It does not authorize training V3, running Phase 2G, or making an optimizer "
            "performance claim. Those require separate frozen gates."
        ),
    )
