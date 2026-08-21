"""Public boundary of the datasets module."""

from octoforge_core.datasets.requests import (
    DatasetDefinition,
    DatasetRankingRequest,
    DatasetRecordQuery,
    DatasetRecordScan,
)
from octoforge_core.datasets.service_port import DatasetService
from octoforge_core.datasets.store_ports import (
    DatasetLexicalSearch,
    DatasetStore,
    DatasetVectorSearch,
)
from octoforge_core.datasets.types import (
    Dataset,
    DatasetExistsError,
    DatasetField,
    DatasetHit,
    DatasetNotFoundError,
    DatasetQuotaError,
    DatasetRecord,
    DatasetRecordValidationError,
    DatasetSchema,
    DatasetSchemaError,
    EmbeddedDataset,
    FieldType,
)

__all__ = [
    "Dataset",
    "DatasetDefinition",
    "DatasetExistsError",
    "DatasetField",
    "DatasetHit",
    "DatasetLexicalSearch",
    "DatasetNotFoundError",
    "DatasetQuotaError",
    "DatasetRankingRequest",
    "DatasetRecord",
    "DatasetRecordQuery",
    "DatasetRecordScan",
    "DatasetRecordValidationError",
    "DatasetSchema",
    "DatasetSchemaError",
    "DatasetService",
    "DatasetStore",
    "DatasetVectorSearch",
    "EmbeddedDataset",
    "FieldType",
]
