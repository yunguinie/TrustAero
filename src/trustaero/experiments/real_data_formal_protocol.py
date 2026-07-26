"""Validate the frozen first formal real-data measurement protocol.

This protocol authorizes method-level measurements on a development month.  It
does not turn January into an unseen optimizer holdout and does not evaluate an
optimizer simply because all legal candidates are timed for an Oracle bound.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trustaero.experiments.bts_mask_join_pilot import (
    MASK_JOIN_FORMAL_LABEL,
    load_bts_mask_join_pilot_config,
)
from trustaero.experiments.real_data_candidate_pilot import (
    FORMAL_CANDIDATE_LABEL,
    load_candidate_pilot_config,
)
from trustaero.experiments.real_data_query_families import load_query_family_protocol


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FormalComponent(_StrictModel):
    component_id: Literal["full-month-materialization", "full-month-mask-placement"]
    config_path: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_template_ids: tuple[str, ...] = Field(min_length=1)
    expected_candidates_per_template: int = Field(ge=2)
    measured_blocks: int = Field(ge=30)


class DeferredTemplate(_StrictModel):
    template_id: str = Field(min_length=1)
    reason: str = Field(min_length=20)


class FormalRealDataProtocol(_StrictModel):
    schema_version: Literal[1]
    protocol_id: str = Field(min_length=1)
    scientific_label: Literal["formal_real_data_development_partition_paper_candidate"]
    partition_role: str = Field(min_length=20)
    frozen_before_measurement: Literal[True]
    optimizer_selection_evaluated: Literal[False]
    reporting_rule: str = Field(min_length=30)
    query_family_design: FileBinding
    components: tuple[FormalComponent, ...] = Field(min_length=1)
    deferred_templates: tuple[DeferredTemplate, ...] = ()

    @model_validator(mode="after")
    def identifiers_are_disjoint(self) -> FormalRealDataProtocol:
        component_ids = [item.component_id for item in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("formal component IDs cannot repeat")
        eligible = [
            template_id
            for component in self.components
            for template_id in component.eligible_template_ids
        ]
        deferred = [item.template_id for item in self.deferred_templates]
        if len(eligible) != len(set(eligible)) or len(deferred) != len(set(deferred)):
            raise ValueError("formal template IDs cannot repeat")
        if set(eligible) & set(deferred):
            raise ValueError("a template cannot be both eligible and deferred")
        return self


class FormalProtocolCheck(_StrictModel):
    protocol_id: str
    status: Literal["PASS"] = "PASS"
    protocol_sha256: str
    eligible_template_ids: tuple[str, ...]
    deferred_template_ids: tuple[str, ...]
    candidate_measurements_per_template: dict[str, int]
    paper_performance_evidence: Literal[True] = True
    heldout_optimizer_evidence: Literal[False] = False
    scientific_boundary: str


def _read_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _resolve_bound_file(root: Path, relative_path: str, digest: str) -> Path:
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"formal protocol path is invalid: {relative_path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"formal protocol SHA-256 changed: {relative_path}")
    return path


def validate_formal_real_data_protocol(
    path: Path | str,
    *,
    project_root: Path,
) -> FormalProtocolCheck:
    """Validate query coverage, component controls, and immutable bindings."""

    protocol_path = Path(path)
    protocol = FormalRealDataProtocol.model_validate(_read_object(protocol_path))
    root = project_root.resolve()
    query_path = _resolve_bound_file(
        root,
        protocol.query_family_design.path,
        protocol.query_family_design.sha256,
    )
    query_protocol = load_query_family_protocol(query_path)
    semantic_ready = {
        item.template_id for item in query_protocol.templates if item.stage == "semantic_ready"
    }
    eligible = tuple(
        template_id
        for component in protocol.components
        for template_id in component.eligible_template_ids
    )
    deferred = tuple(item.template_id for item in protocol.deferred_templates)
    if set(eligible) | set(deferred) != semantic_ready:
        raise ValueError("formal eligible and deferred sets must cover semantic-ready templates")

    measurements: dict[str, int] = {}
    for component in protocol.components:
        config_path = _resolve_bound_file(root, component.config_path, component.config_sha256)
        if component.component_id == "full-month-materialization":
            config = load_candidate_pilot_config(config_path)
            if (
                config.scientific_label != FORMAL_CANDIDATE_LABEL
                or not config.paper_performance_evidence
                or config.measured_runs != component.measured_blocks
                or component.expected_candidates_per_template != 3
            ):
                raise ValueError("formal materialization component controls differ")
        else:
            mask_config = load_bts_mask_join_pilot_config(config_path)
            if (
                mask_config.scientific_label != MASK_JOIN_FORMAL_LABEL
                or not mask_config.paper_performance_evidence
                or mask_config.measured_blocks != component.measured_blocks
                or component.expected_candidates_per_template != 2
            ):
                raise ValueError("formal Mask/Join component controls differ")
        for template_id in component.eligible_template_ids:
            measurements[template_id] = (
                component.measured_blocks * component.expected_candidates_per_template
            )

    return FormalProtocolCheck(
        protocol_id=protocol.protocol_id,
        protocol_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        eligible_template_ids=eligible,
        deferred_template_ids=deferred,
        candidate_measurements_per_template=measurements,
        scientific_boundary=(
            "The protocol authorizes reproducible method-level measurements on the "
            "January development partition. It is not independent Optimizer V1/V2 holdout evidence."
        ),
    )
