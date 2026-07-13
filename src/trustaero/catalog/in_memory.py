"""Deterministic in-memory catalog for unit tests and early experiments."""

from .models import CatalogDocument, DatasetDescriptor


class InMemoryCatalog:
    def __init__(self, document: CatalogDocument) -> None:
        self._datasets = {dataset.dataset_id: dataset for dataset in document.datasets}

    def get_dataset(self, dataset_id: str) -> DatasetDescriptor | None:
        return self._datasets.get(dataset_id)
