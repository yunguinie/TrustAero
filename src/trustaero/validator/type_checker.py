"""Deterministic relation-schema propagation for the supported IR fragment.

This module checks database plan semantics that JSON Schema cannot express:
whether fields still exist after projection, whether operators receive spatial
or temporal inputs, and whether rewrites preserve a valid output schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trustaero.catalog.models import (
    Catalog,
    DataType,
    FieldDescriptor,
    FieldRole,
    SpatialDescriptor,
)
from trustaero.ir.enums import ReasonCode
from trustaero.ir.models import (
    Aggregate,
    CandidatePlan,
    Diagnostic,
    Filter,
    GeneralizeLocation,
    Join,
    LineageCapture,
    Mask,
    MinGroupSize,
    Operator,
    Project,
    ScanSource,
    SpatialFilter,
    SpatialJoin,
    TemporalFilter,
)


@dataclass(frozen=True)
class RelationSchema:
    """Fields and relation-level capabilities after one plan operator."""

    fields: tuple[FieldDescriptor, ...]
    spatial: tuple[SpatialDescriptor, ...] = ()

    def get(self, name: str) -> FieldDescriptor | None:
        """Return a field by its current output name."""

        return next((field for field in self.fields if field.name == name), None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


@dataclass(frozen=True)
class TypeCheckResult:
    """Schemas inferred for each operator and deterministic root diagnostics."""

    outputs: Mapping[str, RelationSchema]
    diagnostics: tuple[Diagnostic, ...]


def _diagnostic(code: ReasonCode, message: str, operator_id: str, **details: Any) -> Diagnostic:
    return Diagnostic(code=code, message=message, operator_id=operator_id, details=details)


def _topological_order(operators: tuple[Operator, ...]) -> tuple[Operator, ...]:
    """Order a graph whose references and acyclicity were already validated."""

    by_id = {operator.operator_id: operator for operator in operators}
    visited: set[str] = set()
    order: list[Operator] = []

    def visit(operator: Operator) -> None:
        if operator.operator_id in visited:
            return
        for input_id in operator.inputs:
            parent = by_id.get(input_id)
            if parent is not None:
                visit(parent)
        visited.add(operator.operator_id)
        order.append(operator)

    for operator in operators:
        visit(operator)
    return tuple(order)


def _project_spatial(
    descriptors: tuple[SpatialDescriptor, ...], selected: set[str]
) -> tuple[SpatialDescriptor, ...]:
    """A coordinate-pair capability survives only when both fields survive."""

    return tuple(descriptor for descriptor in descriptors if descriptor.fields <= selected)


def _merge_inputs(
    operator: Join | SpatialJoin,
    left: RelationSchema,
    right: RelationSchema,
) -> tuple[RelationSchema | None, tuple[Diagnostic, ...]]:
    duplicates = sorted(set(left.names) & set(right.names))
    if duplicates:
        return None, (
            _diagnostic(
                ReasonCode.AMBIGUOUS_OUTPUT_FIELD,
                "Join outputs require aliases when input field names overlap.",
                operator.operator_id,
                fields=duplicates,
            ),
        )
    return RelationSchema(left.fields + right.fields, left.spatial + right.spatial), ()


def _infer_operator(
    operator: Operator,
    inputs: tuple[RelationSchema, ...],
    catalog: Catalog,
) -> tuple[RelationSchema | None, tuple[Diagnostic, ...]]:
    """Apply one operator's transfer rule to already inferred input schemas."""

    if isinstance(operator, ScanSource):
        dataset = catalog.get_dataset(operator.dataset)
        if dataset is None:
            return None, (
                _diagnostic(
                    ReasonCode.UNKNOWN_DATASET,
                    "Dataset is not registered in the catalog.",
                    operator.operator_id,
                    dataset=operator.dataset,
                ),
            )
        if operator.snapshot is not None and operator.snapshot not in dataset.versions:
            return None, (
                _diagnostic(
                    ReasonCode.VERSION_UNRESOLVED,
                    "Requested data snapshot is unavailable.",
                    operator.operator_id,
                    dataset=operator.dataset,
                    snapshot=operator.snapshot,
                ),
            )
        spatial = (dataset.spatial,) if dataset.spatial is not None else ()
        return RelationSchema(dataset.fields, spatial), ()

    input_schema = inputs[0]

    if isinstance(operator, Project):
        duplicates = sorted(
            name for name in set(operator.fields) if operator.fields.count(name) > 1
        )
        if duplicates:
            return None, (
                _diagnostic(
                    ReasonCode.DUPLICATE_OUTPUT_FIELD,
                    "Project output field names must be unique.",
                    operator.operator_id,
                    fields=duplicates,
                ),
            )
        missing = [name for name in operator.fields if input_schema.get(name) is None]
        if missing:
            return None, (
                _diagnostic(
                    ReasonCode.FIELD_NOT_AVAILABLE,
                    "Project references fields absent from its input schema.",
                    operator.operator_id,
                    fields=missing,
                    available_fields=input_schema.names,
                ),
            )
        fields = tuple(input_schema.get(name) for name in operator.fields)
        # The missing-field branch above proves every lookup succeeded.
        projected = tuple(field for field in fields if field is not None)
        selected = set(operator.fields)
        return RelationSchema(projected, _project_spatial(input_schema.spatial, selected)), ()

    if isinstance(operator, SpatialFilter):
        if not input_schema.spatial:
            return None, (
                _diagnostic(
                    ReasonCode.SPATIAL_FIELD_REQUIRED,
                    "SpatialFilter requires an input with a complete spatial descriptor.",
                    operator.operator_id,
                ),
            )
        available_crs = sorted({descriptor.crs for descriptor in input_schema.spatial})
        if operator.crs not in available_crs:
            return None, (
                _diagnostic(
                    ReasonCode.SPATIAL_CRS_MISMATCH,
                    "SpatialFilter CRS does not match its input spatial metadata.",
                    operator.operator_id,
                    requested_crs=operator.crs,
                    available_crs=available_crs,
                ),
            )
        return input_schema, ()

    if isinstance(operator, TemporalFilter):
        field = input_schema.get(operator.field)
        if field is None:
            return None, (
                _diagnostic(
                    ReasonCode.FIELD_NOT_AVAILABLE,
                    "TemporalFilter field is absent from its input schema.",
                    operator.operator_id,
                    field=operator.field,
                    available_fields=input_schema.names,
                ),
            )
        if field.data_type != DataType.DATETIME or FieldRole.TEMPORAL not in field.roles:
            return None, (
                _diagnostic(
                    ReasonCode.TEMPORAL_FIELD_TYPE_INVALID,
                    "TemporalFilter requires a catalog-declared temporal DATETIME field.",
                    operator.operator_id,
                    field=operator.field,
                    data_type=field.data_type.value,
                ),
            )
        if operator.start >= operator.end:
            return None, (
                _diagnostic(
                    ReasonCode.INVALID_TIME_RANGE,
                    "TemporalFilter requires start to be earlier than end.",
                    operator.operator_id,
                ),
            )
        return input_schema, ()

    if isinstance(operator, Join):
        left, right = inputs
        left_key = left.get(operator.left_field)
        right_key = right.get(operator.right_field)
        missing = [
            name
            for name, field in ((operator.left_field, left_key), (operator.right_field, right_key))
            if field is None
        ]
        if missing:
            return None, (
                _diagnostic(
                    ReasonCode.FIELD_NOT_AVAILABLE,
                    "Join key is absent from its corresponding input schema.",
                    operator.operator_id,
                    fields=missing,
                ),
            )
        if left_key is not None and right_key is not None:
            if left_key.data_type != right_key.data_type:
                return None, (
                    _diagnostic(
                        ReasonCode.JOIN_KEY_TYPE_MISMATCH,
                        "Join keys must have the same logical type in IR v1.",
                        operator.operator_id,
                        left_type=left_key.data_type.value,
                        right_type=right_key.data_type.value,
                    ),
                )
        return _merge_inputs(operator, left, right)

    if isinstance(operator, SpatialJoin):
        left, right = inputs
        if not left.spatial or not right.spatial:
            return None, (
                _diagnostic(
                    ReasonCode.SPATIAL_FIELD_REQUIRED,
                    "SpatialJoin requires spatial capability on both inputs.",
                    operator.operator_id,
                    left_has_spatial=bool(left.spatial),
                    right_has_spatial=bool(right.spatial),
                ),
            )
        left_crs = {descriptor.crs for descriptor in left.spatial}
        right_crs = {descriptor.crs for descriptor in right.spatial}
        if left_crs.isdisjoint(right_crs):
            return None, (
                _diagnostic(
                    ReasonCode.SPATIAL_CRS_MISMATCH,
                    "SpatialJoin inputs must share a CRS in IR v1.",
                    operator.operator_id,
                    left_crs=sorted(left_crs),
                    right_crs=sorted(right_crs),
                ),
            )
        return _merge_inputs(operator, left, right)

    if isinstance(operator, GeneralizeLocation):
        missing = [name for name in operator.fields if input_schema.get(name) is None]
        if missing:
            return None, (
                _diagnostic(
                    ReasonCode.FIELD_NOT_AVAILABLE,
                    "GeneralizeLocation field is absent from its input schema.",
                    operator.operator_id,
                    fields=missing,
                    available_fields=input_schema.names,
                ),
            )
        targets = set(operator.fields)
        complete_pair = any(descriptor.fields <= targets for descriptor in input_schema.spatial)
        spatial_roles = all(
            field is not None and FieldRole.SPATIAL in field.roles
            for field in (input_schema.get(name) for name in operator.fields)
        )
        if not complete_pair or not spatial_roles:
            return None, (
                _diagnostic(
                    ReasonCode.GENERALIZATION_TARGET_NOT_SPATIAL,
                    "Generalization requires a complete catalog-declared coordinate pair.",
                    operator.operator_id,
                    fields=operator.fields,
                ),
            )
        updated = tuple(
            field.model_copy(update={"spatial_precision_km": operator.precision_km})
            if field.name in targets
            else field
            for field in input_schema.fields
        )
        return RelationSchema(updated, input_schema.spatial), ()

    if isinstance(operator, Mask):
        missing = [name for name in operator.fields if input_schema.get(name) is None]
        if missing:
            return None, (
                _diagnostic(
                    ReasonCode.FIELD_NOT_AVAILABLE,
                    "Mask field is absent from its input schema.",
                    operator.operator_id,
                    fields=missing,
                ),
            )
        # Mask physical types depend on the chosen method. IR v1 validates
        # field binding but conservatively retains the input logical schema.
        return input_schema, ()

    if isinstance(operator, (MinGroupSize, LineageCapture)):
        return input_schema, ()

    if isinstance(operator, (Filter, Aggregate)):
        return None, (
            _diagnostic(
                ReasonCode.OPERATOR_SEMANTICS_UNSUPPORTED,
                "Free-form expressions cannot be type-checked soundly in IR v1.",
                operator.operator_id,
                operator_type=operator.operator_type,
            ),
        )

    return None, (
        _diagnostic(
            ReasonCode.OPERATOR_SEMANTICS_UNSUPPORTED,
            "Operator has no schema transfer rule in IR v1.",
            operator.operator_id,
            operator_type=operator.operator_type,
        ),
    )


def type_check_plan(
    plan: CandidatePlan,
    catalog: Catalog,
    *,
    operators: tuple[Operator, ...] | None = None,
    output_operator: str | None = None,
) -> TypeCheckResult:
    """Infer output schemas in topological order and fail closed on root errors.

    Graph reference and cycle validation must run first. If an upstream schema
    cannot be inferred, dependent nodes are skipped so users see the root cause
    instead of a cascade of misleading secondary errors.
    """

    checked_operators = operators if operators is not None else plan.operators
    declared_output = output_operator if output_operator is not None else plan.output_operator
    outputs: dict[str, RelationSchema] = {}
    diagnostics: list[Diagnostic] = []
    scanned_fields: set[str] = set()

    for operator in _topological_order(checked_operators):
        input_schemas: list[RelationSchema] = []
        upstream_unavailable = False
        for input_id in operator.inputs:
            schema = outputs.get(input_id)
            if schema is None:
                upstream_unavailable = True
                break
            input_schemas.append(schema)
        if upstream_unavailable:
            continue

        output, errors = _infer_operator(operator, tuple(input_schemas), catalog)
        diagnostics.extend(errors)
        if output is not None:
            outputs[operator.operator_id] = output
            if isinstance(operator, ScanSource):
                scanned_fields.update(output.names)

    final_schema = outputs.get(declared_output)
    if final_schema is not None:
        missing = [
            field for field in plan.requested_output.fields if final_schema.get(field) is None
        ]
        if missing:
            removed = sorted(field for field in missing if field in scanned_fields)
            unknown = sorted(field for field in missing if field not in scanned_fields)
            if removed:
                diagnostics.append(
                    _diagnostic(
                        ReasonCode.FIELD_NOT_AVAILABLE,
                        "Requested fields are absent from the final output schema.",
                        declared_output,
                        fields=removed,
                        available_fields=final_schema.names,
                    )
                )
            if unknown:
                diagnostics.append(
                    _diagnostic(
                        ReasonCode.UNKNOWN_FIELD,
                        "Requested fields do not exist in any scanned dataset.",
                        declared_output,
                        fields=unknown,
                    )
                )

    return TypeCheckResult(outputs=outputs, diagnostics=tuple(diagnostics))
