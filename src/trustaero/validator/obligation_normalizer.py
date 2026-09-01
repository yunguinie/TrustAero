"""Canonicalize policy obligations before deterministic plan rewriting.

The supported obligation types do not share one global numeric order. Each
type has a small, explicit merge algebra. Normalization is intentionally kept
separate from rewriting so rule order cannot change the validated plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from trustaero.ir.enums import LineageLevel, ObligationType, ReasonCode
from trustaero.ir.models import Diagnostic, Obligation


class NormalizationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ObligationMergeRecord:
    """Internal provenance explaining one canonical obligation."""

    obligation: Obligation
    source_policy_ids: tuple[str, ...]
    rule: str


@dataclass(frozen=True)
class ObligationNormalizationResult:
    """Canonical obligations, or diagnostics when no safe merge exists."""

    status: NormalizationStatus
    normalized_obligations: tuple[Obligation, ...]
    diagnostics: tuple[Diagnostic, ...]
    provenance: tuple[ObligationMergeRecord, ...]


@dataclass(frozen=True)
class _SourcedObligation:
    obligation: Obligation
    sources: frozenset[str]


def _diagnostic(code: ReasonCode, message: str, **details: Any) -> Diagnostic:
    return Diagnostic(code=code, message=message, details=details)


def _invalid(obligation: Obligation, message: str, **details: Any) -> Diagnostic:
    return _diagnostic(
        ReasonCode.OBLIGATION_PARAMETER_INVALID,
        message,
        obligation_type=obligation.obligation_type.value,
        parameters=obligation.parameters,
        **details,
    )


def _has_only_keys(params: dict[str, Any], allowed: set[str]) -> bool:
    return set(params) <= allowed


def _fields(value: Any) -> tuple[str, ...] | None:
    """Return a stable non-empty field set without coercing input values."""

    if not isinstance(value, (list, tuple)) or not value:
        return None
    if any(not isinstance(field, str) or not field for field in value):
        return None
    return tuple(sorted(set(value)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        return None
    return int(value)


def _canonicalize(obligation: Obligation) -> tuple[Obligation | None, Diagnostic | None]:
    """Validate supported parameters and make defaults explicit."""

    params = obligation.parameters
    obligation_type = obligation.obligation_type

    if obligation_type == ObligationType.VERSION_PIN:
        if params:
            return None, _invalid(
                obligation,
                "IR v1 VERSION_PIN is parameter-free; fixed versions and ranges are not defined.",
            )
        return obligation.model_copy(update={"parameters": {}}), None

    if obligation_type == ObligationType.MASK:
        if not _has_only_keys(params, {"fields", "method"}):
            return None, _invalid(obligation, "Mask contains unsupported parameters.")
        fields = _fields(params.get("fields"))
        method = params.get("method", "redact")
        if fields is None or method not in {"redact", "hash", "null"}:
            return None, _invalid(obligation, "Mask fields or method are invalid.")
        return obligation.model_copy(
            update={"parameters": {"fields": list(fields), "method": method}}
        ), None

    if obligation_type == ObligationType.GENERALIZE_LOCATION:
        if not _has_only_keys(params, {"fields", "precision_km", "method"}):
            return None, _invalid(
                obligation,
                "GeneralizeLocation contains unsupported parameters.",
            )
        fields = _fields(params.get("fields"))
        precision = _number(params.get("precision_km"))
        method = params.get("method", "fixed_grid")
        if fields is None or precision is None or method != "fixed_grid":
            return None, _invalid(
                obligation,
                "GeneralizeLocation fields, precision, or method are invalid.",
            )
        return obligation.model_copy(
            update={
                "parameters": {
                    "fields": list(fields),
                    "precision_km": precision,
                    "method": method,
                }
            }
        ), None

    if obligation_type == ObligationType.MIN_GROUP_SIZE:
        if not _has_only_keys(params, {"minimum_count"}):
            return None, _invalid(obligation, "MinGroupSize contains unsupported parameters.")
        minimum = _integer(params.get("minimum_count"))
        if minimum is None:
            return None, _invalid(obligation, "MinGroupSize requires an integer of at least 2.")
        return obligation.model_copy(update={"parameters": {"minimum_count": minimum}}), None

    if obligation_type == ObligationType.LINEAGE_CAPTURE:
        if not _has_only_keys(params, {"level"}):
            return None, _invalid(obligation, "LineageCapture contains unsupported parameters.")
        raw_level = params.get("level")
        if not isinstance(raw_level, str):
            return None, _invalid(obligation, "LineageCapture level must be a string.")
        try:
            level = LineageLevel(raw_level)
        except ValueError:
            return None, _invalid(obligation, "LineageCapture level is unsupported.")
        return obligation.model_copy(update={"parameters": {"level": level.value}}), None

    # Unsupported obligation types remain visible so the existing rewriter can
    # reject them explicitly. Normalization must not invent missing semantics.
    ordered_params = dict(sorted(params.items()))
    return obligation.model_copy(update={"parameters": ordered_params}), None


def _parameter_key(obligation: Obligation) -> str:
    return json.dumps(obligation.parameters, sort_keys=True, separators=(",", ":"), default=repr)


def _record(item: _SourcedObligation, rule: str) -> ObligationMergeRecord:
    return ObligationMergeRecord(
        obligation=item.obligation,
        source_policy_ids=tuple(sorted(item.sources)),
        rule=rule,
    )


def _merge_masks(
    items: list[_SourcedObligation],
) -> tuple[list[_SourcedObligation], tuple[Diagnostic, ...]]:
    """Union fields per method and reject incomparable methods on one field."""

    field_requirements: dict[str, dict[str, set[str]]] = {}

    for item in items:
        method = str(item.obligation.parameters["method"])
        for field in item.obligation.parameters["fields"]:
            methods = field_requirements.setdefault(field, {})
            methods.setdefault(method, set()).update(item.sources)

    diagnostics = [
        _diagnostic(
            ReasonCode.MASK_METHOD_CONFLICT,
            "One field has incomparable masking-method requirements.",
            field=field,
            methods=sorted(methods),
            source_policy_ids=sorted(set().union(*(sources for sources in methods.values()))),
        )
        for field, methods in sorted(field_requirements.items())
        if len(methods) > 1
    ]

    if diagnostics:
        return [], tuple(diagnostics)

    method_fields: dict[str, list[str]] = {}
    method_sources: dict[str, set[str]] = {}
    for field, methods in sorted(field_requirements.items()):
        method = next(iter(methods))
        method_fields.setdefault(method, []).append(field)
        method_sources.setdefault(method, set()).update(methods[method])

    merged = [
        _SourcedObligation(
            Obligation(
                obligation_type=ObligationType.MASK,
                parameters={"fields": sorted(fields), "method": method},
            ),
            frozenset(method_sources[method]),
        )
        for method, fields in sorted(method_fields.items())
    ]
    return merged, ()


def _sort_key(item: _SourcedObligation) -> tuple[int, str, str]:
    """Stable logical-suffix order based on the current dependency contract."""

    priority = {
        ObligationType.VERSION_PIN: 0,
        ObligationType.GENERALIZE_LOCATION: 10,
        ObligationType.MASK: 20,
        ObligationType.MIN_GROUP_SIZE: 30,
        ObligationType.LINEAGE_CAPTURE: 40,
    }.get(item.obligation.obligation_type, 90)
    parameter_order = _parameter_key(item.obligation)
    if item.obligation.obligation_type == ObligationType.MASK:
        # Mask methods remain semantically incomparable. This is only a stable
        # tie-break between disjoint targets, not a strength ranking.
        params = item.obligation.parameters
        parameter_order = f"{params['method']}:{','.join(params['fields'])}"
    return (
        priority,
        item.obligation.obligation_type.value,
        parameter_order,
    )


def normalize_obligations(
    obligations: tuple[Obligation, ...],
    source_policy_ids: tuple[str, ...] = (),
) -> ObligationNormalizationResult:
    """Return one deterministic obligation set independent of rule order.

    Current partial orders are: larger fixed-grid cells, larger minimum group
    sizes, and ``none < source < record`` lineage. Mask methods have no assumed
    strength order; overlapping methods therefore conflict.
    """

    if source_policy_ids and len(source_policy_ids) != len(obligations):
        diagnostic = _diagnostic(
            ReasonCode.OBLIGATION_NORMALIZATION_FAILED,
            "Obligation provenance is not aligned with the evaluated obligations.",
            obligation_count=len(obligations),
            source_count=len(source_policy_ids),
        )
        return ObligationNormalizationResult(NormalizationStatus.CONFLICT, (), (diagnostic,), ())
    sources = source_policy_ids or tuple(f"input:{index}" for index in range(len(obligations)))

    canonical: list[_SourcedObligation] = []
    diagnostics: list[Diagnostic] = []
    for obligation, source in zip(obligations, sources, strict=True):
        normalized, error = _canonicalize(obligation)
        if error is not None:
            diagnostics.append(error)
        elif normalized is not None:
            canonical.append(_SourcedObligation(normalized, frozenset((source,))))
    if diagnostics:
        return ObligationNormalizationResult(
            NormalizationStatus.CONFLICT, (), tuple(diagnostics), ()
        )

    output: list[_SourcedObligation] = []
    records_by_key: dict[tuple[str, str], str] = {}

    versions = [
        item for item in canonical if item.obligation.obligation_type == ObligationType.VERSION_PIN
    ]
    if versions:
        merged = _SourcedObligation(
            versions[0].obligation,
            frozenset().union(*(item.sources for item in versions)),
        )
        output.append(merged)
        records_by_key[(ObligationType.VERSION_PIN.value, _parameter_key(merged.obligation))] = (
            "version_pin.deduplicate"
        )

    generalizations = [
        item
        for item in canonical
        if item.obligation.obligation_type == ObligationType.GENERALIZE_LOCATION
    ]
    generalize_groups: dict[tuple[tuple[str, ...], str], list[_SourcedObligation]] = {}
    for item in generalizations:
        params = item.obligation.parameters
        generalize_key = (tuple(params["fields"]), str(params["method"]))
        generalize_groups.setdefault(generalize_key, []).append(item)
    for (fields, method), items in generalize_groups.items():
        precision = max(float(item.obligation.parameters["precision_km"]) for item in items)
        merged = _SourcedObligation(
            Obligation(
                obligation_type=ObligationType.GENERALIZE_LOCATION,
                parameters={
                    "fields": list(fields),
                    "precision_km": precision,
                    "method": method,
                },
            ),
            frozenset().union(*(item.sources for item in items)),
        )
        output.append(merged)
        records_by_key[
            (ObligationType.GENERALIZE_LOCATION.value, _parameter_key(merged.obligation))
        ] = "generalize.max_precision"

    masks, mask_errors = _merge_masks(
        [item for item in canonical if item.obligation.obligation_type == ObligationType.MASK]
    )
    if mask_errors:
        return ObligationNormalizationResult(NormalizationStatus.CONFLICT, (), mask_errors, ())
    for item in masks:
        output.append(item)
        records_by_key[(ObligationType.MASK.value, _parameter_key(item.obligation))] = (
            "mask.union_fields_by_method"
        )

    minimums = [
        item
        for item in canonical
        if item.obligation.obligation_type == ObligationType.MIN_GROUP_SIZE
    ]
    if minimums:
        minimum = max(int(item.obligation.parameters["minimum_count"]) for item in minimums)
        merged = _SourcedObligation(
            Obligation(
                obligation_type=ObligationType.MIN_GROUP_SIZE,
                parameters={"minimum_count": minimum},
            ),
            frozenset().union(*(item.sources for item in minimums)),
        )
        output.append(merged)
        records_by_key[(ObligationType.MIN_GROUP_SIZE.value, _parameter_key(merged.obligation))] = (
            "min_group_size.maximum"
        )

    lineage_items = [
        item
        for item in canonical
        if item.obligation.obligation_type == ObligationType.LINEAGE_CAPTURE
    ]
    if lineage_items:
        strength = {
            LineageLevel.NONE.value: 0,
            LineageLevel.SOURCE.value: 1,
            LineageLevel.RECORD.value: 2,
        }
        level = max(
            (str(item.obligation.parameters["level"]) for item in lineage_items),
            key=strength.__getitem__,
        )
        merged = _SourcedObligation(
            Obligation(
                obligation_type=ObligationType.LINEAGE_CAPTURE,
                parameters={"level": level},
            ),
            frozenset().union(*(item.sources for item in lineage_items)),
        )
        output.append(merged)
        records_by_key[
            (ObligationType.LINEAGE_CAPTURE.value, _parameter_key(merged.obligation))
        ] = "lineage.maximum_level"

    supported = {
        ObligationType.VERSION_PIN,
        ObligationType.MASK,
        ObligationType.GENERALIZE_LOCATION,
        ObligationType.MIN_GROUP_SIZE,
        ObligationType.LINEAGE_CAPTURE,
    }
    unsupported_groups: dict[tuple[str, str], list[_SourcedObligation]] = {}
    for item in canonical:
        if item.obligation.obligation_type in supported:
            continue
        unsupported_key = (
            item.obligation.obligation_type.value,
            _parameter_key(item.obligation),
        )
        unsupported_groups.setdefault(unsupported_key, []).append(item)
    for unsupported_key, items in unsupported_groups.items():
        merged = _SourcedObligation(
            items[0].obligation,
            frozenset().union(*(item.sources for item in items)),
        )
        output.append(merged)
        records_by_key[unsupported_key] = "exact.deduplicate"

    ordered = tuple(sorted(output, key=_sort_key))
    provenance = tuple(
        _record(
            item,
            records_by_key[
                (item.obligation.obligation_type.value, _parameter_key(item.obligation))
            ],
        )
        for item in ordered
    )
    return ObligationNormalizationResult(
        NormalizationStatus.SUCCESS,
        tuple(item.obligation for item in ordered),
        (),
        provenance,
    )
