"""Mechanism-calibrated cost model for legal Mask placements.

The model is deliberately small and auditable.  It estimates SHA-256 work,
payload materialization, and hash-Join row processing independently.  It never
uses a workload winner as an input feature and never makes an illegal plan
eligible merely because that plan has a lower estimated runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trustaero.optimizer.mask import MaskPlacement, MaskPlacementFeatures

HASH_FEATURE_NAMES = ("rows_100k", "input_bytes_mib")
MATERIALIZATION_FEATURE_NAMES = ("rows_100k", "payload_bytes_mib")
JOIN_FEATURE_NAMES = ("input_rows_100k", "output_rows_100k")


@dataclass(frozen=True)
class NonnegativeMechanismCost:
    """One non-negative linear component cost measured in milliseconds."""

    component_name: str
    feature_names: tuple[str, ...]
    intercept_ms: float
    coefficients: tuple[float, ...]
    ridge_lambda: float
    training_group_count: int
    source_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.component_name:
            raise ValueError("Mechanism component_name cannot be empty")
        if not self.feature_names or len(self.feature_names) != len(self.coefficients):
            raise ValueError("Mechanism feature names and coefficients are incompatible")
        if self.intercept_ms < 0.0 or any(value < 0.0 for value in self.coefficients):
            raise ValueError("Mechanism costs and coefficients must be non-negative")
        if self.ridge_lambda < 0.0 or self.training_group_count <= 0:
            raise ValueError("Mechanism fitting metadata is invalid")
        if not self.source_run_ids:
            raise ValueError("Mechanism model must retain at least one source run ID")

    def predict_ms(self, values: tuple[float, ...]) -> float:
        """Predict component time while rejecting an incompatible feature vector."""

        if len(values) != len(self.coefficients):
            raise ValueError("Mechanism feature vector has an incompatible length")
        if any(value < 0.0 for value in values):
            raise ValueError("Mechanism feature values must be non-negative")
        return self.intercept_ms + sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, values, strict=True)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_name": self.component_name,
            "feature_names": list(self.feature_names),
            "intercept_ms": self.intercept_ms,
            "coefficients": list(self.coefficients),
            "ridge_lambda": self.ridge_lambda,
            "training_group_count": self.training_group_count,
            "source_run_ids": list(self.source_run_ids),
            "coefficient_constraint": "non_negative",
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NonnegativeMechanismCost:
        try:
            return cls(
                component_name=str(payload["component_name"]),
                feature_names=tuple(str(value) for value in payload["feature_names"]),
                intercept_ms=float(payload["intercept_ms"]),
                coefficients=tuple(float(value) for value in payload["coefficients"]),
                ridge_lambda=float(payload["ridge_lambda"]),
                training_group_count=int(payload["training_group_count"]),
                source_run_ids=tuple(str(value) for value in payload["source_run_ids"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed non-negative mechanism cost") from error


@dataclass(frozen=True)
class MechanismMaskCostModel:
    """Three separately calibrated operation costs for Mask placement."""

    hash_cost: NonnegativeMechanismCost
    materialization_cost: NonnegativeMechanismCost
    join_cost: NonnegativeMechanismCost
    hashed_identifier_width_bytes: int = 64

    def __post_init__(self) -> None:
        expected = (
            (self.hash_cost, "sha256", HASH_FEATURE_NAMES),
            (
                self.materialization_cost,
                "materialization_roundtrip",
                MATERIALIZATION_FEATURE_NAMES,
            ),
            (self.join_cost, "hash_join", JOIN_FEATURE_NAMES),
        )
        for component, name, features in expected:
            if component.component_name != name or component.feature_names != features:
                raise ValueError(f"Mechanism model has an incompatible {name} component")
        if self.hashed_identifier_width_bytes <= 0:
            raise ValueError("hashed_identifier_width_bytes must be positive")

    @staticmethod
    def _row_units(rows: float) -> float:
        return rows / 100_000.0

    @staticmethod
    def _byte_units(rows: float, width_bytes: int) -> float:
        return rows * width_bytes / float(1024 * 1024)

    def estimate_hash_ms(self, rows: float, width_bytes: int) -> float:
        """Estimate production-compatible SHA-256 work for a byte payload."""

        return self.hash_cost.predict_ms(
            (self._row_units(rows), self._byte_units(rows, width_bytes))
        )

    def estimate_materialization_ms(self, rows: float, width_bytes: int) -> float:
        """Estimate one explicit/intermediate payload movement boundary."""

        return self.materialization_cost.predict_ms(
            (self._row_units(rows), self._byte_units(rows, width_bytes))
        )

    def estimate_join_ms(self, input_rows: float, output_rows: float) -> float:
        """Estimate DuckDB HASH_JOIN work without forcing a width coefficient."""

        return self.join_cost.predict_ms(
            (self._row_units(input_rows), self._row_units(output_rows))
        )

    def candidate_components_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> dict[str, float]:
        """Explain the frozen early/late formula using measured mechanisms.

        Early Mask hashes every input, materializes the narrow hash, then
        joins.  Late Mask joins raw identifiers, moves only matching raw
        payload, and hashes only matching rows.  The same cardinality-only
        Join term appears in both plans; payload width is represented by the
        separately measured materialization term.
        """

        input_rows = float(features.join_input_rows)
        output_rows = input_rows * features.join_match_rate
        raw_width = features.identifier_width_bytes
        join_ms = self.estimate_join_ms(input_rows, output_rows)
        if placement is MaskPlacement.EARLY:
            hash_ms = self.estimate_hash_ms(input_rows, raw_width)
            movement_ms = self.estimate_materialization_ms(
                input_rows, self.hashed_identifier_width_bytes
            )
        else:
            hash_ms = self.estimate_hash_ms(output_rows, raw_width)
            movement_ms = self.estimate_materialization_ms(output_rows, raw_width)
        return {
            "sha256_ms": hash_ms,
            "payload_movement_ms": movement_ms,
            "hash_join_ms": join_ms,
        }

    def predict_candidate_ms(
        self,
        features: MaskPlacementFeatures,
        placement: MaskPlacement,
    ) -> float:
        return sum(self.candidate_components_ms(features, placement).values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "mechanism_mask_cost_v1",
            "formula_schema_version": 1,
            "target_unit": "milliseconds",
            "hashed_identifier_width_bytes": self.hashed_identifier_width_bytes,
            "hash_cost": self.hash_cost.to_dict(),
            "materialization_cost": self.materialization_cost.to_dict(),
            "join_cost": self.join_cost.to_dict(),
            "scientific_boundary": (
                "Candidate legality and raw-exposure limits are hard feasibility "
                "constraints evaluated before runtime cost."
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MechanismMaskCostModel:
        if payload.get("model_type") != "mechanism_mask_cost_v1":
            raise ValueError("Unsupported mechanism Mask cost model_type")
        try:
            hash_payload = payload["hash_cost"]
            materialization_payload = payload["materialization_cost"]
            join_payload = payload["join_cost"]
            if not all(
                isinstance(value, dict)
                for value in (hash_payload, materialization_payload, join_payload)
            ):
                raise TypeError("Mechanism components must be objects")
            return cls(
                hash_cost=NonnegativeMechanismCost.from_dict(hash_payload),
                materialization_cost=NonnegativeMechanismCost.from_dict(
                    materialization_payload
                ),
                join_cost=NonnegativeMechanismCost.from_dict(join_payload),
                hashed_identifier_width_bytes=int(
                    payload["hashed_identifier_width_bytes"]
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Malformed mechanism Mask cost artifact") from error


@dataclass(frozen=True)
class MechanismMaskCostDecision:
    """Auditable, feasibility-aware decision from the mechanism model."""

    placement: MaskPlacement
    reason_code: str
    estimated_early_latency_ms: float
    estimated_late_latency_ms: float
    early_components_ms: dict[str, float]
    late_components_ms: dict[str, float]
    estimated_early_raw_exposure_rows: int
    estimated_late_raw_exposure_rows: int


def choose_mask_placement_by_mechanism(
    features: MaskPlacementFeatures,
    model: MechanismMaskCostModel,
) -> MechanismMaskCostDecision:
    """Exclude illegal candidates before comparing estimated mechanism costs."""

    early_components = model.candidate_components_ms(features, MaskPlacement.EARLY)
    late_components = model.candidate_components_ms(features, MaskPlacement.LATE)
    early_ms = sum(early_components.values())
    late_ms = sum(late_components.values())
    early_feasible = features.early_mask_legal
    late_feasible = features.late_mask_legal
    if features.max_raw_exposure_rows is not None:
        late_feasible = late_feasible and (
            features.join_input_rows <= features.max_raw_exposure_rows
        )
    if not early_feasible and not late_feasible:
        raise ValueError("No legal Mask placement satisfies the governance constraints")
    if early_feasible and not late_feasible:
        placement = MaskPlacement.EARLY
        reason = "MASK_MECHANISM_LATE_INFEASIBLE"
    elif late_feasible and not early_feasible:
        placement = MaskPlacement.LATE
        reason = "MASK_MECHANISM_EARLY_INFEASIBLE"
    elif early_ms < late_ms:
        placement = MaskPlacement.EARLY
        reason = "MASK_MECHANISM_EARLY_ESTIMATED_CHEAPER"
    else:
        placement = MaskPlacement.LATE
        reason = "MASK_MECHANISM_LATE_ESTIMATED_CHEAPER_OR_TIE"
    return MechanismMaskCostDecision(
        placement=placement,
        reason_code=reason,
        estimated_early_latency_ms=early_ms,
        estimated_late_latency_ms=late_ms,
        early_components_ms=early_components,
        late_components_ms=late_components,
        estimated_early_raw_exposure_rows=0,
        estimated_late_raw_exposure_rows=features.join_input_rows,
    )
