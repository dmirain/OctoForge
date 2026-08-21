"""Read and compactly render inferred collection schemas."""

from octoforge_core.net.collections.schema_nodes import (
    TYPE_ARRAY,
    TYPE_OBJECT,
    SchemaNode,
)

MAX_RENDERED_FIELDS = 40
MAX_RENDER_DEPTH = 4


def field_node(schema: SchemaNode, path: str) -> SchemaNode | None:
    """Resolve a dotted path against a record schema; return None when absent."""
    node = schema
    for part in path.split("."):
        if node.get("type") != TYPE_OBJECT:
            return None
        fields: dict[str, SchemaNode] = node.get("fields", {})
        found = fields.get(part)
        if found is None:
            return None
        node = found
    return node


def known_fields(schema: SchemaNode) -> list[str]:
    """Return sorted top-level field names for query errors."""
    if schema.get("type") != TYPE_OBJECT:
        return []
    return sorted(schema.get("fields", {}))


def render(schema: SchemaNode, depth: int = 0) -> str:
    """Return the compact human/model-facing form of a schema node."""
    kind = schema.get("type")
    suffix = "?" if schema.get("optional") else ""
    nullable = "|null" if schema.get("nullable") else ""
    if kind == TYPE_OBJECT:
        return _render_object(schema, depth, nullable)
    if kind == TYPE_ARRAY:
        element = schema.get("element")
        inner = render(element, depth + 1) if element is not None else "?"
        return f"array of {inner}{nullable}{suffix}"
    return f"{kind}{nullable}{suffix}"


def _render_object(schema: SchemaNode, depth: int, nullable: str) -> str:
    if depth >= MAX_RENDER_DEPTH:
        return "{…}"
    fields: dict[str, SchemaNode] = schema.get("fields", {})
    names = sorted(fields)
    shown = names[:MAX_RENDERED_FIELDS]
    parts = [_render_field(name, fields[name], depth) for name in shown]
    if len(names) > len(shown):
        parts.append(f"… +{len(names) - len(shown)} fields")
    return "{" + ", ".join(parts) + "}" + nullable


def _render_field(name: str, node: SchemaNode, depth: int) -> str:
    suffix = "?" if node.get("optional") else ""
    return f"{name}{suffix}: {render(_plain(node), depth + 1)}"


def _plain(node: SchemaNode) -> SchemaNode:
    if node.get("optional"):
        trimmed = dict(node)
        trimmed.pop("optional")
        return trimmed
    return node
