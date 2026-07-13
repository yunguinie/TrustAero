"""Typed dataset metadata used by plan-semantic validation.

The catalog is the authority for field types and spatial/temporal capability.
The validator must never infer those properties from convenient field names
such as ``latitude`` or ``geometry``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, model_validator

from trustaero.ir.enums import DataType
from trustaero.ir.models import StrictModel


class FieldRole(StrEnum):
    """Semantic capabilities independent of a field's physical type."""

    IDENTIFIER = "identifier"
    SPATIAL = "spatial"
    TEMPORAL = "temporal"


class FieldDescriptor(StrictModel):
    """Catalog declaration for one relation field.

    A field may have multiple roles. For example, a floating-point coordinate
    can be both spatial and sensitive; sensitivity remains a separate
    governance property rather than being mixed into the type system.
    """

    name: str = Field(min_length=1)
    data_type: DataType
    nullable: bool = False
    roles: frozenset[FieldRole] = frozenset()
    sensitive: bool = False
    spatial_precision_km: float | None = Field(default=None, gt=0)
    # ``raw`` means later operators may still rely on the field's original
    # value semantics. Masked states are presentation states: they can be
    # projected, but they no longer carry join/filter/aggregate/spatial/temporal
    # capability in the trusted IR fragment.
    value_state: Literal["raw", "redacted", "hashed", "nullified"] = "raw"


class SpatialDescriptor(StrictModel):
    """A coordinate pair that jointly gives a relation spatial capability."""

    latitude_field: str
    longitude_field: str
    crs: str

    @property
    def fields(self) -> frozenset[str]:
        return frozenset((self.latitude_field, self.longitude_field))


class DatasetDescriptor(StrictModel):
    """Versioned relation metadata required by the validator."""

    dataset_id: str
    versions: tuple[str, ...]
    default_version: str
    fields: tuple[FieldDescriptor, ...]
    spatial: SpatialDescriptor | None = None
    temporal_field: str | None = None

    @model_validator(mode="after")
    def metadata_must_be_self_consistent(self) -> DatasetDescriptor:
        """Reject catalog drift before it can influence authorization."""

        if self.default_version not in self.versions:
            raise ValueError("default_version must be listed in versions")
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("dataset field names must be unique")
        by_name = {field.name: field for field in self.fields}

        if self.spatial is not None:
            missing = sorted(self.spatial.fields - by_name.keys())
            if missing:
                raise ValueError(f"spatial descriptor references missing fields: {missing}")
            for name in self.spatial.fields:
                field = by_name[name]
                if field.data_type != DataType.FLOAT or FieldRole.SPATIAL not in field.roles:
                    raise ValueError("coordinate fields must be FLOAT fields with the SPATIAL role")

        if self.temporal_field is not None:
            temporal = by_name.get(self.temporal_field)
            if temporal is None:
                raise ValueError("temporal_field must reference an existing field")
            if temporal.data_type != DataType.DATETIME or FieldRole.TEMPORAL not in temporal.roles:
                raise ValueError("temporal_field must be DATETIME with the TEMPORAL role")
        return self


class CatalogDocument(StrictModel):
    schema_version: str = Field(pattern=r"^1\.0$")
    datasets: tuple[DatasetDescriptor, ...]


class Catalog(Protocol):
    """Minimal interface that future DuckDB/PostGIS catalogs can implement."""

    def get_dataset(self, dataset_id: str) -> DatasetDescriptor | None: ...
