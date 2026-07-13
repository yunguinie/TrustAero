"""Catalog types kept separate from validator rules.

目录隔离可以避免把实验数据集的字段、版本和敏感等级硬编码进验证器。
"""

from typing import Protocol

from pydantic import Field

from trustaero.ir.models import StrictModel


class FieldDescriptor(StrictModel):
    name: str
    data_type: str
    sensitive: bool = False


class DatasetDescriptor(StrictModel):
    dataset_id: str
    versions: tuple[str, ...]
    default_version: str
    fields: tuple[FieldDescriptor, ...]
    spatial_field: str | None = None
    temporal_field: str | None = None


class CatalogDocument(StrictModel):
    schema_version: str = Field(pattern=r"^1\.0$")
    datasets: tuple[DatasetDescriptor, ...]


class Catalog(Protocol):
    """Minimal interface that future DuckDB/PostGIS catalogs can implement."""

    def get_dataset(self, dataset_id: str) -> DatasetDescriptor | None: ...
