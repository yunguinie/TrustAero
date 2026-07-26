"""Model-free admission audit for the final multi-candidate optimizer.

The audit deliberately runs before another optimizer is implemented.  Having
many SQL strings is not enough: the accepted evidence must show that governance
changes the legal set and that at least one three-or-more-candidate query family
has different winners across predeclared strata.  Otherwise a model can appear
accurate by memorizing a workload name or a fixed default.
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
class AdmissionSource:
    """One immutable experiment or design input."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class AdmissionGates:
    """Predeclared conditions that must hold before optimizer training."""

    minimum_structural_three_candidate_families: int
    minimum_accepted_performance_families: int
    minimum_global_winner_classes: int
    minimum_three_candidate_families_with_internal_winner_diversity: int
    require_governance_candidate_pruning: bool
    require_development_bidirectional_reversal: bool
    require_real_bidirectional_reversal: bool


@dataclass(frozen=True, slots=True)
class MultiCandidateAdmissionConfig:
    """Source-bound audit configuration."""

    protocol_id: str
    results_dir: str
    sources: dict[str, AdmissionSource]
    gates: AdmissionGates
    require_clean_git: bool
    scientific_boundary: str


def load_multicandidate_admission_config(
    path: Path | str,
) -> MultiCandidateAdmissionConfig:
    """Load and type-check the frozen JSON configuration."""

    payload = _load_json(Path(path))
    sources = {
        str(name): AdmissionSource(
            path=str(cast(dict[str, Any], value)["path"]),
            sha256=str(cast(dict[str, Any], value)["sha256"]),
        )
        for name, value in cast(dict[str, Any], payload["sources"]).items()
    }
    gate_payload = cast(dict[str, Any], payload["gates"])
    return MultiCandidateAdmissionConfig(
        protocol_id=str(payload["protocol_id"]),
        results_dir=str(payload["results_dir"]),
        sources=sources,
        gates=AdmissionGates(
            minimum_structural_three_candidate_families=int(
                gate_payload["minimum_structural_three_candidate_families"]
            ),
            minimum_accepted_performance_families=int(
                gate_payload["minimum_accepted_performance_families"]
            ),
            minimum_global_winner_classes=int(gate_payload["minimum_global_winner_classes"]),
            minimum_three_candidate_families_with_internal_winner_diversity=int(
                gate_payload["minimum_three_candidate_families_with_internal_winner_diversity"]
            ),
            require_governance_candidate_pruning=bool(
                gate_payload["require_governance_candidate_pruning"]
            ),
            require_development_bidirectional_reversal=bool(
                gate_payload["require_development_bidirectional_reversal"]
            ),
            require_real_bidirectional_reversal=bool(
                gate_payload["require_real_bidirectional_reversal"]
            ),
        ),
        require_clean_git=bool(payload["require_clean_git"]),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _structural_families(
    query_protocol: dict[str, Any],
) -> tuple[dict[str, dict[str, object]], bool]:
    """Return semantic-ready 3+ candidate families and policy-pruning evidence."""

    families: dict[str, dict[str, object]] = {}
    governance_prunes = False
    for template in cast(list[dict[str, Any]], query_protocol["templates"]):
        if str(template["stage"]) != "semantic_ready":
            continue
        profile_sets: list[tuple[str, ...]] = []
        rejected: set[str] = set()
        candidates: set[str] = set()
        for profile in cast(list[dict[str, Any]], template["governance_profiles"]):
            feasible = tuple(str(item) for item in profile["expected_feasible_candidates"])
            profile_sets.append(feasible)
            candidates.update(feasible)
            rejected.update(str(item) for item in profile["expected_rejected_candidates"])
        if len({item for item in profile_sets}) > 1 or rejected:
            governance_prunes = True
        if len(candidates) >= 3:
            template_id = str(template["template_id"])
            families[template_id] = {
                "workload_id": str(template["workload_id"]),
                "candidate_ids": sorted(candidates),
                "candidate_count": len(candidates),
                "governance_profile_count": len(profile_sets),
            }
    return families, governance_prunes


def _accepted_labels(
    real_acceptance: dict[str, Any],
    multijoin_acceptance: dict[str, Any],
    tpch_acceptance: dict[str, Any],
) -> list[dict[str, object]]:
    """Normalize accepted labels without treating a diagnostic tie as a winner."""

    labels: list[dict[str, object]] = []
    unit_to_template = {
        "bts-full-2024-01": "QF-BTS-MASKED-READ",
        "nyc_tlc-full-2024-01": "QF-NYC-ZONE-AGGREGATE",
    }
    grouped_real_labels: dict[tuple[str, tuple[str, ...]], int] = {}
    for row in cast(list[dict[str, Any]], real_acceptance["observations"]):
        template_id = unit_to_template[str(row["unit_id"])]
        oracle = tuple(sorted(str(item) for item in row["oracle_set_within_3_percent"]))
        # Two policy profiles can expose the same performance label.  Count the
        # physical unit once so policy duplication cannot inflate diversity,
        # while retaining the complete permissive candidate count.
        key = (template_id, oracle)
        grouped_real_labels[key] = max(
            grouped_real_labels.get(key, 0),
            len(row["feasible_candidate_ids"]),
        )
    for (template_id, oracle), candidate_count in sorted(grouped_real_labels.items()):
        labels.append(
            {
                "template_id": template_id,
                "candidate_count": candidate_count,
                "oracle_set": list(oracle),
                "baseline_id": "fused",
            }
        )

    for template_id, source in (
        ("QF-BTS-NATURAL-MULTIJOIN", multijoin_acceptance),
        ("QF-TPCH-Q6", tpch_acceptance),
    ):
        oracle = tuple(
            sorted(str(item) for item in source["diagnostic_oracle_set_within_tie_band"])
        )
        labels.append(
            {
                "template_id": template_id,
                "candidate_count": len(source["median_candidate_over_fused_ratio"]),
                "oracle_set": list(oracle),
                "baseline_id": "fused",
            }
        )
    return labels


def _winner_class(label: dict[str, object]) -> str:
    oracle = tuple(str(item) for item in cast(list[object], label["oracle_set"]))
    baseline = str(label["baseline_id"])
    if len(oracle) != 1:
        return "TIE_OR_INCONCLUSIVE"
    return "BASELINE" if oracle[0] == baseline else "NONBASELINE"


def audit_multicandidate_admission(
    query_protocol: dict[str, Any],
    real_acceptance: dict[str, Any],
    multijoin_acceptance: dict[str, Any],
    tpch_acceptance: dict[str, Any],
    development_reversal: dict[str, Any],
    real_reversal: dict[str, Any],
    gates: AdmissionGates,
) -> dict[str, object]:
    """Decide whether a non-trivial multi-candidate optimizer is justified."""

    structural, governance_prunes = _structural_families(query_protocol)
    labels = _accepted_labels(real_acceptance, multijoin_acceptance, tpch_acceptance)

    accepted_sources = (
        real_acceptance["status"] == "PASS",
        multijoin_acceptance["status"] == "PASS",
        tpch_acceptance["status"] == "PASS",
    )
    labels_by_family: dict[str, list[dict[str, object]]] = {}
    for label in labels:
        labels_by_family.setdefault(str(label["template_id"]), []).append(label)

    diverse_three_candidate_families: list[str] = []
    for template_id, family_labels in sorted(labels_by_family.items()):
        singleton_winners = {
            str(cast(list[object], label["oracle_set"])[0])
            for label in family_labels
            if template_id in structural
            and cast(int, label["candidate_count"]) >= 3
            and len(cast(list[object], label["oracle_set"])) == 1
        }
        if len(singleton_winners) >= 2:
            diverse_three_candidate_families.append(template_id)

    global_winner_classes = sorted(
        {_winner_class(label) for label in labels if _winner_class(label) != "TIE_OR_INCONCLUSIVE"}
    )
    development_bidirectional = (
        int(development_reversal["policy_first_winner_count"]) > 0
        and int(development_reversal["query_first_winner_count"]) > 0
        and development_reversal["reversal_discovery"] == "STABLE_BIDIRECTIONAL_REVERSAL_DISCOVERED"
    )
    real_bidirectional = (
        int(real_reversal["policy_first_winner_count"]) > 0
        and int(real_reversal["query_first_winner_count"]) > 0
        and real_reversal["reversal_discovery"] == "STABLE_BIDIRECTIONAL_REVERSAL_DISCOVERED"
    )
    checks = {
        "all_performance_sources_accepted": all(accepted_sources),
        "minimum_structural_three_candidate_families": (
            len(structural) >= gates.minimum_structural_three_candidate_families
        ),
        "minimum_accepted_performance_families": (
            len(labels_by_family) >= gates.minimum_accepted_performance_families
        ),
        "minimum_global_winner_classes": (
            len(global_winner_classes) >= gates.minimum_global_winner_classes
        ),
        "minimum_three_candidate_families_with_internal_winner_diversity": (
            len(diverse_three_candidate_families)
            >= gates.minimum_three_candidate_families_with_internal_winner_diversity
        ),
        "governance_candidate_pruning": (
            governance_prunes or not gates.require_governance_candidate_pruning
        ),
        "development_bidirectional_reversal": (
            development_bidirectional or not gates.require_development_bidirectional_reversal
        ),
        "real_bidirectional_reversal": (
            real_bidirectional or not gates.require_real_bidirectional_reversal
        ),
    }
    authorized = all(checks.values())
    return {
        "schema_version": 1,
        "status": (
            "PASS_MULTICANDIDATE_OPTIMIZER_ADMISSION"
            if authorized
            else "FAIL_MULTICANDIDATE_OPTIMIZER_ADMISSION_RETAIN"
        ),
        "optimizer_training_authorized": authorized,
        "structural_three_candidate_families": structural,
        "accepted_performance_labels": labels,
        "accepted_performance_family_count": len(labels_by_family),
        "global_winner_classes": global_winner_classes,
        "three_candidate_families_with_internal_winner_diversity": (
            diverse_three_candidate_families
        ),
        "development_bidirectional_reversal": development_bidirectional,
        "real_bidirectional_reversal": real_bidirectional,
        "governance_candidate_pruning_observed": governance_prunes,
        "gate_checks": checks,
        "failed_gates": sorted(name for name, passed in checks.items() if not passed),
        "authorized_next_step": (
            "Implement the unified multi-candidate optimizer."
            if authorized
            else (
                "Before fitting a model, extend one governance-driven checkpoint "
                "family to at least three legal, physically distinct candidates "
                "and measure a frozen development grid that can establish "
                "within-family winner diversity."
            )
        ),
        "paper_optimizer_performance_claim_authorized": False,
    }


def run_multicandidate_admission(
    config: MultiCandidateAdmissionConfig,
    *,
    project_root: Path,
    config_path: Path,
) -> Path:
    """Verify source hashes, execute the audit, and persist its provenance."""

    root = project_root.resolve()
    loaded: dict[str, dict[str, Any]] = {}
    for name, binding in config.sources.items():
        source_path = root / binding.path
        if sha256_file(source_path) != binding.sha256:
            raise ValueError(f"Multi-candidate admission source changed: {binding.path}")
        loaded[name] = _load_json(source_path)
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise ValueError("Multi-candidate admission requires a clean Git worktree")

    result = audit_multicandidate_admission(
        loaded["query_protocol"],
        loaded["real_acceptance"],
        loaded["multijoin_acceptance"],
        loaded["tpch_acceptance"],
        loaded["development_reversal"],
        loaded["real_reversal"],
        config.gates,
    )
    result["scientific_boundary"] = config.scientific_boundary
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
