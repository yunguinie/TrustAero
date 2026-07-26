"""Audit whether an optimizer workload can support a multi-factor claim.

This audit deliberately fits no new optimizer.  It checks whether the frozen
development labels contain evidence that cannot be explained by Join match
rate alone.  A pipeline-aware model is not scientifically justified until the
workload passes this gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.experiments.real_data_governed import _atomic_json, _load_json
from trustaero.experiments.real_data_pilot import _git_state
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class WorkloadSufficiencyGates:
    minimum_stable_family_count: int
    minimum_query_template_count: int
    minimum_fixed_match_rate_reversal_strata: int
    maximum_match_rate_baseline_top1: float
    minimum_identifier_width_levels: int
    minimum_complete_time_groups: int


@dataclass(frozen=True, slots=True)
class WorkloadSufficiencyConfig:
    protocol_name: str
    results_dir: str
    paired_stability_audit_path: str
    paired_stability_audit_sha256: str
    v41_result_path: str
    v41_result_sha256: str
    query_template_ids: tuple[str, ...]
    require_clean_git: bool
    gates: WorkloadSufficiencyGates
    scientific_boundary: str


def load_workload_sufficiency_config(
    path: Path | str,
) -> WorkloadSufficiencyConfig:
    """Load the immutable audit inputs and evidence thresholds."""

    payload = _load_json(Path(path))
    return WorkloadSufficiencyConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        paired_stability_audit_path=str(payload["paired_stability_audit_path"]),
        paired_stability_audit_sha256=str(payload["paired_stability_audit_sha256"]),
        v41_result_path=str(payload["v41_result_path"]),
        v41_result_sha256=str(payload["v41_result_sha256"]),
        query_template_ids=tuple(str(item) for item in payload["query_template_ids"]),
        require_clean_git=bool(payload["require_clean_git"]),
        gates=WorkloadSufficiencyGates(**cast(dict[str, Any], payload["gates"])),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def audit_workload_sufficiency(
    paired_audit: dict[str, Any],
    v41_result: dict[str, Any],
    config: WorkloadSufficiencyConfig,
) -> dict[str, object]:
    """Return a deterministic, model-free workload sufficiency decision."""

    families = cast(list[dict[str, Any]], paired_audit["family_audits"])
    stable = [item for item in families if item["stable_for_model_evaluation"]]
    strata: dict[float, list[dict[str, Any]]] = {}
    for family in stable:
        strata.setdefault(float(family["target_match_rate"]), []).append(family)

    stratum_rows: list[dict[str, object]] = []
    reversal_count = 0
    for rate in sorted(strata):
        members = strata[rate]
        directions = [str(item["directions"]["overall"]) for item in members]
        early = directions.count("early_mask_materialized")
        late = directions.count("late_mask")
        contains_reversal = early > 0 and late > 0
        reversal_count += int(contains_reversal)
        stratum_rows.append(
            {
                "target_match_rate": rate,
                "stable_family_count": len(members),
                "early_preferred_count": early,
                "late_preferred_count": late,
                "contains_stable_direction_reversal": contains_reversal,
            }
        )

    match_metrics = cast(dict[str, Any], v41_result["deployed_metrics"]["match_rate_baseline"])
    width_levels = sorted({int(item["identifier_width_bytes"]) for item in families})
    time_groups = sorted({str(item["scenario_group"]) for item in families})
    template_ids = sorted(set(config.query_template_ids))
    match_top1 = float(match_metrics["top1_selection_rate"])
    checks = {
        "minimum_stable_family_count": (len(stable) >= config.gates.minimum_stable_family_count),
        "minimum_query_template_count": (
            len(template_ids) >= config.gates.minimum_query_template_count
        ),
        "minimum_fixed_match_rate_reversal_strata": (
            reversal_count >= config.gates.minimum_fixed_match_rate_reversal_strata
        ),
        "maximum_match_rate_baseline_top1": (
            match_top1 <= config.gates.maximum_match_rate_baseline_top1
        ),
        "minimum_identifier_width_levels": (
            len(width_levels) >= config.gates.minimum_identifier_width_levels
        ),
        "minimum_complete_time_groups": (
            len(time_groups) >= config.gates.minimum_complete_time_groups
        ),
        "source_structural_gate_passed": (paired_audit["status"] == "PASS_PAIRED_STABILITY_AUDIT"),
        "no_external_partition_access": (
            not bool(paired_audit["external_partition_accessed"])
            and not bool(v41_result["external_partition_accessed"])
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "status": (
            "PASS_WORKLOAD_DISCRIMINATIVE_SUFFICIENCY"
            if passed
            else "FAIL_WORKLOAD_DISCRIMINATIVE_SUFFICIENCY"
        ),
        "stable_family_count": len(stable),
        "query_template_ids": template_ids,
        "identifier_width_levels": width_levels,
        "complete_time_groups": time_groups,
        "fixed_match_rate_strata": stratum_rows,
        "fixed_match_rate_reversal_strata": reversal_count,
        "match_rate_baseline_top1": match_top1,
        "match_rate_baseline_mean_regret_percent": float(match_metrics["mean_regret_percent"]),
        "gate_checks": checks,
        "pipeline_model_authorized": passed,
        "external_partition_accessed": False,
        "required_next_workload_properties": [
            "at least two governance-driven query templates",
            "stable early and late winners within a fixed match-rate stratum",
            "complete time-group isolation during cross-validation",
            "all compared candidates must pass governance legality first",
            "match-rate-only baseline must remain explicit",
        ],
        "scientific_boundary": config.scientific_boundary,
    }


def run_workload_sufficiency_audit(
    config: WorkloadSufficiencyConfig,
    *,
    project_root: Path,
    config_path: Path,
) -> Path:
    """Verify bound inputs, run the audit, and persist an immutable-style run."""

    root = project_root.resolve()
    paired_path = root / config.paired_stability_audit_path
    v41_path = root / config.v41_result_path
    if sha256_file(paired_path) != config.paired_stability_audit_sha256:
        raise ValueError("Paired stability audit binding changed")
    if sha256_file(v41_path) != config.v41_result_sha256:
        raise ValueError("V4.1 result binding changed")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Workload sufficiency audit requires a clean commit")
    result = audit_workload_sufficiency(
        _load_json(paired_path),
        _load_json(v41_path),
        config,
    )
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
    _atomic_json(run_dir / "audit.json", result)
    _atomic_json(run_dir.parent / "latest_run.json", {"run_id": run_id})
    return run_dir
