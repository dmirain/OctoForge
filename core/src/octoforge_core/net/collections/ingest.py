"""Stable public facade for shape-aware response ingestion."""

from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionPassport,
    CollectionStore,
    NewRecords,
)
from octoforge_core.net.collections.ingest_csv import parse_csv as _parse_csv
from octoforge_core.net.collections.ingest_models import ParsedBody, SpillRequest
from octoforge_core.net.collections.ingest_parse import (
    CSV_CONTENT_MARKERS,
    JSON_CONTENT_MARKERS,
    PARSE_IN_THREAD_CHARS,
    dotted_get,
    parse_structured,
)
from octoforge_core.net.collections.ingest_parse import (
    take_apart as _take_apart,
)
from octoforge_core.net.collections.ingest_passport import (
    KILOBYTE,
    MAX_ENVELOPE_CHARS,
    MEGABYTE,
    PASSPORT_TEMPLATE,
    TRUNCATED_NOTE,
    _human_size,
    render_passport,
)
from octoforge_core.net.collections.ingest_schema import (
    INFER_IN_THREAD_RECORDS,
    _coerce,
    shape_records,
)
from octoforge_core.net.collections.ingest_schema import (
    infer_schema as _infer,
)
from octoforge_core.net.collections.ingest_schema import (
    merge_schema as _merge,
)
from octoforge_core.net.collections.ingest_sink import CollectionSink, CollectionSinkOptions
from octoforge_core.net.collections.ingest_spill import (
    DB_WIRE_LIMIT_BYTES,
    ResponseSpill,
    ResponseSpillOptions,
)
from octoforge_core.net.collections.ingest_unwrap import (
    MAX_RECORDS,
    MAX_UNWRAP_DEPTH,
    MAX_UNWRAP_NODES,
    _envelope_of,
    _is_object_array,
    _locate_records,
)
from octoforge_core.net.collections.ingest_unwrap import (
    as_records as _as_records,
)
from octoforge_core.net.collections.ingest_unwrap import (
    scalars_of as _scalars_of,
)
from octoforge_core.net.collections.ingest_unwrap import (
    unwrap as _unwrap,
)
from octoforge_core.net.response_memory import DocumentHome, ResponseMemory
from octoforge_core.net.tool_spec import FieldCoercion, ResponseSpec

__all__ = [
    "CSV_CONTENT_MARKERS",
    "DB_WIRE_LIMIT_BYTES",
    "INFER_IN_THREAD_RECORDS",
    "JSON_CONTENT_MARKERS",
    "KILOBYTE",
    "MAX_ENVELOPE_CHARS",
    "MAX_RECORDS",
    "MAX_UNWRAP_DEPTH",
    "MAX_UNWRAP_NODES",
    "MEGABYTE",
    "PARSE_IN_THREAD_CHARS",
    "PASSPORT_TEMPLATE",
    "TRUNCATED_NOTE",
    "CollectionConfig",
    "CollectionKind",
    "CollectionPassport",
    "CollectionSink",
    "CollectionSinkOptions",
    "CollectionStore",
    "DocumentHome",
    "FieldCoercion",
    "NewRecords",
    "ParsedBody",
    "ResponseMemory",
    "ResponseSpec",
    "ResponseSpill",
    "ResponseSpillOptions",
    "SpillRequest",
    "_as_records",
    "_coerce",
    "_envelope_of",
    "_human_size",
    "_infer",
    "_is_object_array",
    "_locate_records",
    "_merge",
    "_parse_csv",
    "_scalars_of",
    "_take_apart",
    "_unwrap",
    "dotted_get",
    "parse_structured",
    "render_passport",
    "shape_records",
]
