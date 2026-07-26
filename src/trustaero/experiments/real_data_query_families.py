"""Freeze and validate governance-driven real-data query families.

The query-family file is a scientific protocol, not a performance result.  It
separates templates that already have an executable TrustAero plan from design
commitments that still need an adapter.  A design-only template is useful for
preventing post-hoc query selection, but it is never allowed into timing runs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.experiments.bts_mask_join import BTS_MASK_JOIN_TARGET
from trustaero.experiments.bts_multijoin import BTS_MULTIJOIN_TARGETS
from trustaero.experiments.real_data_candidates import _TARGETS
from trustaero.ir.enums import LineageLevel
from trustaero.ir.models import PolicySet
from trustaero.planner import generate_duckdb_candidates
from trustaero.validator.service import validate

QUERY_FAMILY_LABEL = "governance_driven_query_family_design_frozen_before_performance"
_REVIEWED_TARGETS = {**_TARGETS, "bts_multijoin": BTS_MULTIJOIN_TARGETS}


class _StrictProtocolModel(BaseModel):
    """Reject unknown protocol fields so typos cannot silently change a study."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GovernanceProfileExpectation(_StrictProtocolModel):
    """Expected hard-constraint outcome before any candidate cost is inspected."""

    profile_id: str = Field(min_length=1)
    expected_feasible_candidates: tuple[str, ...] = ()
    expected_rejected_candidates: tuple[str, ...] = ()

    @model_validator(mode="after")
    def candidates_must_be_disjoint(self) -> GovernanceProfileExpectation:
        feasible = set(self.expected_feasible_candidates)
        rejected = set(self.expected_rejected_candidates)
        if feasible & rejected:
            raise ValueError("a candidate cannot be both feasible and rejected")
        if len(feasible) != len(self.expected_feasible_candidates) or len(rejected) != len(
            self.expected_rejected_candidates
        ):
            raise ValueError("candidate expectations cannot contain duplicates")
        return self


class QueryFamilyTemplate(_StrictProtocolModel):
    """One query shape selected for a governance reason, not for observed speed."""

    template_id: str = Field(pattern=r"^QF-[A-Z0-9-]+$")
    workload_id: str = Field(min_length=1)
    stage: Literal["semantic_ready", "design_only"]
    observed_or_controlled: Literal["observed", "controlled_governance_augmentation"]
    query_shape: str = Field(min_length=1)
    governance_question: str = Field(min_length=1)
    inclusion_reason: str = Field(min_length=1)
    plan_path: str | None = None
    catalog_path: str = "examples/real_data/catalog.json"
    policy_path: str = "examples/real_data/policy.json"
    expected_validation_status: Literal["ACCEPT", "REWRITE"] | None = None
    required_datasets: tuple[str, ...] = ()
    required_operator_types: tuple[str, ...] = ()
    required_mechanisms: tuple[
        Literal["VERSION_PIN", "MASK", "SOURCE_LINEAGE", "RECORD_LINEAGE"], ...
    ] = ()
    candidate_generation: Literal["materialization", "mask_placement"] = "materialization"
    materialization_targets: tuple[str, ...] = ()
    mask_placement_target: str | None = None
    governance_profiles: tuple[GovernanceProfileExpectation, ...] = ()
    implementation_blocker: str | None = None
    performance_eligible: bool = False

    @model_validator(mode="after")
    def stage_fields_must_be_consistent(self) -> QueryFamilyTemplate:
        if self.stage == "semantic_ready":
            if not self.plan_path or not self.expected_validation_status:
                raise ValueError("semantic-ready templates require a plan and expected status")
            if self.implementation_blocker is not None:
                raise ValueError("semantic-ready templates cannot retain an implementation blocker")
            if self.candidate_generation == "materialization" and not self.materialization_targets:
                raise ValueError("materialization templates need reviewed candidate boundaries")
            if self.candidate_generation == "mask_placement" and not self.mask_placement_target:
                raise ValueError("Mask-placement templates need a reviewed placement target")
        else:
            if self.plan_path is not None or self.expected_validation_status is not None:
                raise ValueError("design-only templates cannot claim an executable plan")
            if not self.implementation_blocker:
                raise ValueError("design-only templates must name their blocker")
            if self.performance_eligible:
                raise ValueError("design-only templates cannot be performance eligible")
        if self.performance_eligible:
            # Performance eligibility is a later, explicit freeze.  Merely having
            # executable semantics is not enough to authorize measurements.
            raise ValueError("this design protocol cannot authorize performance timing")
        if len(set(self.required_datasets)) != len(self.required_datasets):
            raise ValueError("required datasets cannot contain duplicates")
        if len(set(self.materialization_targets)) != len(self.materialization_targets):
            raise ValueError("materialization targets cannot contain duplicates")
        if self.candidate_generation == "materialization" and self.mask_placement_target:
            raise ValueError("materialization templates cannot declare a Mask-placement target")
        if self.candidate_generation == "mask_placement" and self.materialization_targets:
            raise ValueError("Mask-placement templates cannot declare materialization targets")
        profile_ids = [item.profile_id for item in self.governance_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("governance profile IDs cannot contain duplicates")
        return self


class RealDataQueryFamilyProtocol(_StrictProtocolModel):
    """Versioned study design whose contents must precede performance results."""

    schema_version: Literal[1]
    protocol_id: str = Field(min_length=1)
    scientific_label: Literal["governance_driven_query_family_design_frozen_before_performance"]
    frozen_before_performance: Literal[True]
    selection_principle: str = Field(min_length=1)
    reporting_rule: str = Field(min_length=1)
    templates: tuple[QueryFamilyTemplate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def template_ids_must_be_unique(self) -> RealDataQueryFamilyProtocol:
        ids = [item.template_id for item in self.templates]
        if len(ids) != len(set(ids)):
            raise ValueError("query-family template IDs must be unique")
        return self


class QueryTemplateCheck(_StrictProtocolModel):
    template_id: str
    stage: Literal["semantic_ready", "design_only"]
    status: Literal["PASS", "PENDING"]
    performance_eligible: Literal[False] = False
    validation_status: str | None = None
    candidate_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


class QueryFamilyProtocolCheck(_StrictProtocolModel):
    protocol_id: str
    status: Literal["PASS"] = "PASS"
    protocol_sha256: str
    semantic_ready_count: int
    design_only_count: int
    performance_ready: Literal[False] = False
    scientific_boundary: str
    templates: tuple[QueryTemplateCheck, ...]


def _read_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def load_query_family_protocol(path: Path | str) -> RealDataQueryFamilyProtocol:
    """Load a frozen protocol with strict schema validation."""

    return RealDataQueryFamilyProtocol.model_validate(_read_json_object(Path(path)))


def _resolve_project_file(project_root: Path, relative_path: str) -> Path:
    """Resolve a tracked input without permitting paths outside the repository."""

    candidate = (project_root / relative_path).resolve()
    if not candidate.is_relative_to(project_root.resolve()):
        raise ValueError(f"protocol path escapes the project root: {relative_path}")
    if not candidate.is_file():
        raise ValueError(f"protocol file does not exist: {relative_path}")
    return candidate


def _check_semantic_template(
    template: QueryFamilyTemplate,
    *,
    project_root: Path,
) -> QueryTemplateCheck:
    if template.plan_path is None or template.expected_validation_status is None:
        raise AssertionError("Pydantic stage validation should have rejected this template")
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(
            _read_json_object(_resolve_project_file(project_root, template.catalog_path))
        )
    )
    policy = PolicySet.model_validate(
        _read_json_object(_resolve_project_file(project_root, template.policy_path))
    )
    plan = _read_json_object(_resolve_project_file(project_root, template.plan_path))
    response = validate(plan, policy, catalog)
    if response.status.value != template.expected_validation_status:
        raise ValueError(
            f"{template.template_id} expected {template.expected_validation_status}, "
            f"received {response.status.value}"
        )
    logical = response.validated_plan
    if logical is None:
        raise ValueError(f"{template.template_id} produced no validated logical plan")

    datasets = set(logical.bindings.data_snapshots)
    if datasets != set(template.required_datasets):
        raise ValueError(f"{template.template_id} dataset binding mismatch: {sorted(datasets)}")
    operator_types = {operator.operator_type for operator in logical.operators}
    missing_types = set(template.required_operator_types) - operator_types
    if missing_types:
        raise ValueError(f"{template.template_id} is missing operators: {sorted(missing_types)}")
    operator_ids = {operator.operator_id for operator in logical.operators}
    declared_targets = set(template.materialization_targets)
    if template.mask_placement_target is not None:
        declared_targets.add(template.mask_placement_target)
    missing_targets = declared_targets - operator_ids
    if missing_targets:
        raise ValueError(
            f"{template.template_id} has unknown candidate boundaries: {sorted(missing_targets)}"
        )

    if "VERSION_PIN" in template.required_mechanisms and not logical.bindings.data_snapshots:
        raise ValueError(f"{template.template_id} has no version bindings")
    if "MASK" in template.required_mechanisms and "Mask" not in operator_types:
        raise ValueError(f"{template.template_id} has no enforced Mask")
    lineage_levels = {requirement.level for requirement in logical.lineage_requirements}
    if (
        "SOURCE_LINEAGE" in template.required_mechanisms
        and LineageLevel.SOURCE not in lineage_levels
    ):
        raise ValueError(f"{template.template_id} has no source-lineage requirement")
    if (
        "RECORD_LINEAGE" in template.required_mechanisms
        and LineageLevel.RECORD not in lineage_levels
    ):
        raise ValueError(f"{template.template_id} has no record-lineage requirement")

    if template.candidate_generation == "materialization":
        workload_targets = _REVIEWED_TARGETS.get(template.workload_id)
        if workload_targets is None or tuple(template.materialization_targets) != workload_targets:
            raise ValueError(
                f"{template.template_id} candidate boundaries differ from reviewed planner targets"
            )
        candidates = generate_duckdb_candidates(
            logical,
            materialization_targets=template.materialization_targets,
        )
    else:
        masks = [operator for operator in logical.operators if operator.operator_type == "Mask"]
        if (
            template.workload_id != "bts_mask_join"
            or template.mask_placement_target != BTS_MASK_JOIN_TARGET
            or len(masks) != 1
        ):
            raise ValueError(f"{template.template_id} Mask placement is not reviewed")
        candidates = generate_duckdb_candidates(
            logical,
            operator_placements=((masks[0].operator_id, template.mask_placement_target),),
        )
    candidate_ids = tuple(candidate.strategy.strategy_id for candidate in candidates)
    for profile in template.governance_profiles:
        expected_ids = set(profile.expected_feasible_candidates) | set(
            profile.expected_rejected_candidates
        )
        if expected_ids != set(candidate_ids):
            raise ValueError(
                f"{template.template_id} profile candidates do not match generated candidates"
            )
    return QueryTemplateCheck(
        template_id=template.template_id,
        stage=template.stage,
        status="PASS",
        validation_status=response.status.value,
        candidate_ids=candidate_ids,
    )


def validate_query_family_protocol(
    protocol_path: Path | str,
    *,
    project_root: Path,
) -> QueryFamilyProtocolCheck:
    """Check frozen design and executable templates without touching dataset rows."""

    path = Path(protocol_path)
    protocol = load_query_family_protocol(path)
    root = project_root.resolve()
    checks: list[QueryTemplateCheck] = []
    for template in protocol.templates:
        if template.stage == "design_only":
            checks.append(
                QueryTemplateCheck(
                    template_id=template.template_id,
                    stage=template.stage,
                    status="PENDING",
                    diagnostics=(template.implementation_blocker or "unspecified blocker",),
                )
            )
        else:
            checks.append(
                _check_semantic_template(
                    template,
                    project_root=root,
                )
            )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    semantic_ready = sum(item.stage == "semantic_ready" for item in protocol.templates)
    return QueryFamilyProtocolCheck(
        protocol_id=protocol.protocol_id,
        protocol_sha256=digest,
        semantic_ready_count=semantic_ready,
        design_only_count=len(protocol.templates) - semantic_ready,
        scientific_boundary=(
            "The study design is frozen, but performance remains unauthorized until "
            "each timed template has an executable plan, semantic-equivalence smoke, "
            "clean source commit, and a separate measurement freeze."
        ),
        templates=tuple(checks),
    )
