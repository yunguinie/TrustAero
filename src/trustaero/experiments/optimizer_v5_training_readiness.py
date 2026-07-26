"""Model-free readiness audit for Optimizer V5 real-data labels.

The audit separates three ideas that must not be conflated:

* a timing protocol completed successfully;
* governance leaves a candidate as the only legal choice;
* several legal candidates exist and measurements identify a faster route.

Only the third item provides discriminative cost-selection evidence.  This
module therefore refuses to authorize another optimizer merely because a
minimum number of rows or governance-forced decisions exists.
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
class TrainingReadinessGates:
    minimum_performance_unit_count: int
    minimum_workload_family_count: int
    minimum_query_template_count: int
    minimum_multi_candidate_unit_count: int
    minimum_distinct_label_class_count: int
    minimum_baseline_winner_count: int
    minimum_unrestricted_nonbaseline_winner_count: int
    minimum_governance_forced_nonbaseline_count: int
    maximum_dominant_label_fraction: float


@dataclass(frozen=True, slots=True)
class TrainingReadinessConfig:
    protocol_name: str
    results_dir: str
    pairwise_inference_path: str
    pairwise_inference_sha256: str
    pairwise_summary_path: str
    pairwise_summary_sha256: str
    multijoin_acceptance_path: str
    multijoin_acceptance_sha256: str
    mask_join_acceptance_path: str
    mask_join_acceptance_sha256: str
    query_family_protocol_path: str
    query_family_protocol_sha256: str
    v5_unit_template_ids: dict[str, str]
    multijoin_template_id: str
    mask_join_template_id: str
    require_clean_git: bool
    gates: TrainingReadinessGates
    scientific_boundary: str


def load_training_readiness_config(path: Path | str) -> TrainingReadinessConfig:
    """Load the source-bound readiness protocol."""

    payload = _load_json(Path(path))
    gates = cast(dict[str, Any], payload["gates"])
    return TrainingReadinessConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        pairwise_inference_path=str(payload["pairwise_inference_path"]),
        pairwise_inference_sha256=str(payload["pairwise_inference_sha256"]),
        pairwise_summary_path=str(payload["pairwise_summary_path"]),
        pairwise_summary_sha256=str(payload["pairwise_summary_sha256"]),
        multijoin_acceptance_path=str(payload["multijoin_acceptance_path"]),
        multijoin_acceptance_sha256=str(payload["multijoin_acceptance_sha256"]),
        mask_join_acceptance_path=str(payload["mask_join_acceptance_path"]),
        mask_join_acceptance_sha256=str(payload["mask_join_acceptance_sha256"]),
        query_family_protocol_path=str(payload["query_family_protocol_path"]),
        query_family_protocol_sha256=str(payload["query_family_protocol_sha256"]),
        v5_unit_template_ids={
            str(key): str(value)
            for key, value in cast(dict[str, Any], payload["v5_unit_template_ids"]).items()
        },
        multijoin_template_id=str(payload["multijoin_template_id"]),
        mask_join_template_id=str(payload["mask_join_template_id"]),
        require_clean_git=bool(payload["require_clean_git"]),
        gates=TrainingReadinessGates(
            minimum_performance_unit_count=int(gates["minimum_performance_unit_count"]),
            minimum_workload_family_count=int(gates["minimum_workload_family_count"]),
            minimum_query_template_count=int(gates["minimum_query_template_count"]),
            minimum_multi_candidate_unit_count=int(gates["minimum_multi_candidate_unit_count"]),
            minimum_distinct_label_class_count=int(gates["minimum_distinct_label_class_count"]),
            minimum_baseline_winner_count=int(gates["minimum_baseline_winner_count"]),
            minimum_unrestricted_nonbaseline_winner_count=int(
                gates["minimum_unrestricted_nonbaseline_winner_count"]
            ),
            minimum_governance_forced_nonbaseline_count=int(
                gates["minimum_governance_forced_nonbaseline_count"]
            ),
            maximum_dominant_label_fraction=float(gates["maximum_dominant_label_fraction"]),
        ),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _label_class(oracle_set: tuple[str, ...], baseline_id: str) -> str:
    """Describe a label without coupling the audit to candidate names."""

    if oracle_set == (baseline_id,):
        return "BASELINE_ONLY"
    if len(oracle_set) == 1:
        return "NONBASELINE_SINGLETON"
    if baseline_id in oracle_set:
        return "PRACTICAL_TIE_INCLUDING_BASELINE"
    return "NONBASELINE_SET"


def _v5_performance_units(
    inference: dict[str, Any], config: TrainingReadinessConfig
) -> list[dict[str, object]]:
    """Collapse duplicate policy rows when they imply the same unit label."""

    grouped: dict[tuple[str, tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for row in cast(list[dict[str, Any]], inference["profile_labels"]):
        if not bool(row["model_label_authorized"]):
            continue
        unit_id = str(row["unit_id"])
        oracle = tuple(str(item) for item in row["authorized_oracle_set"])
        feasible = tuple(str(item) for item in row["feasible_candidate_ids"])
        grouped.setdefault((unit_id, oracle, feasible), []).append(str(row["policy_profile"]))

    units: list[dict[str, object]] = []
    for (unit_id, oracle, feasible), profiles in sorted(grouped.items()):
        template_id = config.v5_unit_template_ids.get(unit_id)
        if template_id is None:
            raise ValueError(f"No frozen query-template mapping for {unit_id}")
        baseline_id = "fused"
        workload = unit_id.rsplit("-n", 1)[0]
        units.append(
            {
                "unit_id": unit_id,
                "workload_family": workload,
                "query_template_id": template_id,
                "evidence_source": "v5_connection_isolated_pairwise",
                "policy_profiles": sorted(profiles),
                "feasible_candidate_ids": list(feasible),
                "baseline_id": baseline_id,
                "authorized_oracle_set": list(oracle),
                "label_class": _label_class(oracle, baseline_id),
                "multi_candidate_choice": len(feasible) >= 2,
            }
        )
    return units


def audit_optimizer_v5_training_readiness(
    pairwise_inference: dict[str, Any],
    pairwise_summary: dict[str, Any],
    multijoin_acceptance: dict[str, Any],
    mask_join_acceptance: dict[str, Any],
    query_family_protocol: dict[str, Any],
    config: TrainingReadinessConfig,
) -> dict[str, object]:
    """Determine whether accepted real-data labels can train a selector."""

    performance_units = _v5_performance_units(pairwise_inference, config)
    if bool(multijoin_acceptance["formal_paper_experiment_authorized"]):
        multijoin_oracle = tuple(
            str(item) for item in multijoin_acceptance["diagnostic_oracle_set_within_tie_band"]
        )
        multijoin_ratios = cast(
            dict[str, Any], multijoin_acceptance["median_candidate_over_fused_ratio"]
        )
        feasible = tuple(sorted(str(item) for item in multijoin_ratios))
        performance_units.append(
            {
                "unit_id": "bts_multijoin-full-january",
                "workload_family": "bts_multijoin",
                "query_template_id": config.multijoin_template_id,
                "evidence_source": "bts_multijoin_formal",
                "policy_profiles": ["source-lineage"],
                "feasible_candidate_ids": list(feasible),
                "baseline_id": "fused",
                "authorized_oracle_set": list(multijoin_oracle),
                "label_class": _label_class(multijoin_oracle, "fused"),
                "multi_candidate_choice": len(feasible) >= 2,
            }
        )

    if bool(mask_join_acceptance["formal_paper_experiment_authorized"]):
        mask_oracle = tuple(
            str(item) for item in mask_join_acceptance["diagnostic_oracle_set_within_tie_band"]
        )
        feasible = ("early_mask_before_join", "late_mask_fused")
        performance_units.append(
            {
                "unit_id": "bts_mask_join-full-january",
                "workload_family": "bts_mask_join",
                "query_template_id": config.mask_join_template_id,
                "evidence_source": "bts_mask_join_formal",
                "policy_profiles": ["raw-join-permitted"],
                "feasible_candidate_ids": list(feasible),
                "baseline_id": "late_mask_fused",
                "authorized_oracle_set": list(mask_oracle),
                "label_class": _label_class(mask_oracle, "late_mask_fused"),
                "multi_candidate_choice": True,
            }
        )

    forced_nonbaseline = 0
    strict_feasible = tuple(
        str(item) for item in mask_join_acceptance["strict_policy_feasible_set"]
    )
    if strict_feasible and "late_mask_fused" not in strict_feasible:
        forced_nonbaseline = 1

    template_rows = cast(list[dict[str, Any]], query_family_protocol["templates"])
    known_templates = {str(item["template_id"]): str(item["stage"]) for item in template_rows}
    used_templates = sorted({str(item["query_template_id"]) for item in performance_units})
    templates_semantically_frozen = all(
        known_templates.get(template_id) == "semantic_ready" for template_id in used_templates
    )
    workload_families = sorted({str(item["workload_family"]) for item in performance_units})
    label_counts: dict[str, int] = {}
    for item in performance_units:
        label = str(item["label_class"])
        label_counts[label] = label_counts.get(label, 0) + 1
    dominant_fraction = (
        max(label_counts.values()) / len(performance_units) if performance_units else 1.0
    )
    baseline_winners = sum(item["label_class"] == "BASELINE_ONLY" for item in performance_units)
    unrestricted_nonbaseline_winners = sum(
        item["label_class"] == "NONBASELINE_SINGLETON" and bool(item["multi_candidate_choice"])
        for item in performance_units
    )
    multi_candidate_units = sum(bool(item["multi_candidate_choice"]) for item in performance_units)
    external_accessed = any(
        bool(source.get("external_partition_accessed", False))
        for source in (pairwise_inference, pairwise_summary)
    ) or any(
        bool(source.get("heldout_optimizer_evidence", False))
        for source in (multijoin_acceptance, mask_join_acceptance)
    )

    gates = config.gates
    checks = {
        "pairwise_label_gate_passed": (
            pairwise_inference["status"] == "PASS_V5_PAIRWISE_LABEL_GATE"
        ),
        "accepted_formal_sources": (
            multijoin_acceptance["status"] == "PASS" and mask_join_acceptance["status"] == "PASS"
        ),
        "templates_semantically_frozen": templates_semantically_frozen,
        "minimum_performance_unit_count": (
            len(performance_units) >= gates.minimum_performance_unit_count
        ),
        "minimum_workload_family_count": (
            len(workload_families) >= gates.minimum_workload_family_count
        ),
        "minimum_query_template_count": (len(used_templates) >= gates.minimum_query_template_count),
        "minimum_multi_candidate_unit_count": (
            multi_candidate_units >= gates.minimum_multi_candidate_unit_count
        ),
        "minimum_distinct_label_class_count": (
            len(label_counts) >= gates.minimum_distinct_label_class_count
        ),
        "minimum_baseline_winner_count": (baseline_winners >= gates.minimum_baseline_winner_count),
        "minimum_unrestricted_nonbaseline_winner_count": (
            unrestricted_nonbaseline_winners >= gates.minimum_unrestricted_nonbaseline_winner_count
        ),
        "minimum_governance_forced_nonbaseline_count": (
            forced_nonbaseline >= gates.minimum_governance_forced_nonbaseline_count
        ),
        "maximum_dominant_label_fraction": (
            dominant_fraction <= gates.maximum_dominant_label_fraction
        ),
        "no_external_partition_access": not external_accessed,
    }
    authorized = all(checks.values())
    return {
        "schema_version": 1,
        "status": (
            "PASS_OPTIMIZER_V5_TRAINING_READINESS"
            if authorized
            else "FAIL_OPTIMIZER_V5_TRAINING_READINESS"
        ),
        "optimizer_v5_training_authorized": authorized,
        "performance_units": performance_units,
        "performance_unit_count": len(performance_units),
        "workload_families": workload_families,
        "query_template_ids": used_templates,
        "label_class_counts": dict(sorted(label_counts.items())),
        "dominant_label_fraction": dominant_fraction,
        "baseline_winner_count": baseline_winners,
        "unrestricted_nonbaseline_winner_count": unrestricted_nonbaseline_winners,
        "governance_forced_nonbaseline_count": forced_nonbaseline,
        "multi_candidate_unit_count": multi_candidate_units,
        "gate_checks": checks,
        "external_partition_accessed": external_accessed,
        "required_next_evidence": [
            "an accepted real-data development unit with at least two legal candidates",
            "a confidence-authorized non-baseline singleton winner",
            "query-family inclusion fixed by governance semantics before timing",
        ],
        "scientific_boundary": config.scientific_boundary,
    }


def run_optimizer_v5_training_readiness_audit(
    config: TrainingReadinessConfig,
    *,
    project_root: Path,
    config_path: Path,
) -> Path:
    """Verify immutable inputs, run the audit, and persist compact evidence."""

    root = project_root.resolve()
    bindings = (
        (config.pairwise_inference_path, config.pairwise_inference_sha256),
        (config.pairwise_summary_path, config.pairwise_summary_sha256),
        (config.multijoin_acceptance_path, config.multijoin_acceptance_sha256),
        (config.mask_join_acceptance_path, config.mask_join_acceptance_sha256),
        (config.query_family_protocol_path, config.query_family_protocol_sha256),
    )
    for relative_path, expected_hash in bindings:
        if sha256_file(root / relative_path) != expected_hash:
            raise ValueError(f"Training-readiness source changed: {relative_path}")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Training-readiness audit requires a clean commit")

    result = audit_optimizer_v5_training_readiness(
        _load_json(root / config.pairwise_inference_path),
        _load_json(root / config.pairwise_summary_path),
        _load_json(root / config.multijoin_acceptance_path),
        _load_json(root / config.mask_join_acceptance_path),
        _load_json(root / config.query_family_protocol_path),
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
