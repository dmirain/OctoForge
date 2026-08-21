"""Stable interface for collection-schema inference and rendering.

The schema document is data (stored in ``collections.schema``), shaped for
the query compiler and for the compact passport shown to the model.
"""

from octoforge_core.net.collections.schema_nodes import (
    TYPE_ARRAY,
    TYPE_BOOLEAN,
    TYPE_NULL,
    TYPE_NUMBER,
    TYPE_OBJECT,
    TYPE_STRING,
    SchemaNode,
    infer_records,
    infer_value,
    merge_nodes,
)
from octoforge_core.net.collections.schema_render import (
    MAX_RENDER_DEPTH,
    MAX_RENDERED_FIELDS,
    field_node,
    known_fields,
    render,
)

__all__ = [
    "MAX_RENDERED_FIELDS",
    "MAX_RENDER_DEPTH",
    "TYPE_ARRAY",
    "TYPE_BOOLEAN",
    "TYPE_NULL",
    "TYPE_NUMBER",
    "TYPE_OBJECT",
    "TYPE_STRING",
    "SchemaNode",
    "field_node",
    "infer_records",
    "infer_value",
    "known_fields",
    "merge_nodes",
    "render",
]
