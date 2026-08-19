"""Derives the schema of a collection from its records, and renders it.

The schema document is data (stored in `collections.schema`), shaped for two
readers: the query compiler validates field paths and picks casts against it,
and the model reads its rendered form in the passport instead of a truncated
body — knowing the shape of 1400 records is worth more per token than seeing
one and a half of them.

Inference is a fold: every record merges into the running node. A field seen
in some records but not all becomes optional; a field seen with two scalar
types degrades to "string" (the honest lowest common denominator — the engine
then compares it textually). Arrays inside records keep only their element
shape and are not addressable by the DSL in v1.
"""

from typing import Any

#: node["type"] values.
TYPE_OBJECT = "object"
TYPE_ARRAY = "array"
TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_NULL = "null"

#: How many fields of one object the render lists before folding the rest.
MAX_RENDERED_FIELDS = 40
#: Depth the render descends to; deeper shapes fold into "…".
MAX_RENDER_DEPTH = 4

SchemaNode = dict[str, Any]


def infer_value(value: Any) -> SchemaNode:  # noqa: ANN401 — JSON is dynamically shaped
    """The schema node of one JSON value."""
    if value is None:
        return {"type": TYPE_NULL}
    if isinstance(value, bool):
        return {"type": TYPE_BOOLEAN}
    if isinstance(value, int | float):
        return {"type": TYPE_NUMBER}
    if isinstance(value, list):
        element: SchemaNode | None = None
        for item in value:
            element = merge_nodes(element, infer_value(item))
        return {"type": TYPE_ARRAY, "element": element}
    if isinstance(value, dict):
        return {
            "type": TYPE_OBJECT,
            "fields": {str(key): infer_value(item) for key, item in value.items()},
        }
    return {"type": TYPE_STRING}  # strings, and exotic types arriving stringified


def merge_nodes(current: SchemaNode | None, incoming: SchemaNode) -> SchemaNode:
    """Fold one more observation into the running node.

    Null is transparent: a field that is sometimes null keeps its real type
    and gains `nullable` instead of degrading to string.
    """
    if current is None:
        return incoming
    if TYPE_NULL in (current["type"], incoming["type"]):
        solid = incoming if current["type"] == TYPE_NULL else current
        return {**solid, "nullable": True} if solid["type"] != TYPE_NULL else solid
    if current["type"] != incoming["type"]:
        return _keep_flags({"type": TYPE_STRING}, current, incoming)
    if current["type"] == TYPE_OBJECT:
        return _keep_flags(_merge_objects(current, incoming), current, incoming)
    if current["type"] == TYPE_ARRAY:
        element = current.get("element")
        other = incoming.get("element")
        merged = element if other is None else merge_nodes(element, other)
        return _keep_flags({"type": TYPE_ARRAY, "element": merged}, current, incoming)
    return _keep_flags(dict(incoming), current, incoming)


def _keep_flags(node: SchemaNode, *sources: SchemaNode) -> SchemaNode:
    if any(source.get("nullable") for source in sources):
        node["nullable"] = True
    if any(source.get("optional") for source in sources):
        node["optional"] = True
    return node


def _merge_objects(current: SchemaNode, incoming: SchemaNode) -> SchemaNode:
    ours: dict[str, SchemaNode] = current["fields"]
    theirs: dict[str, SchemaNode] = incoming["fields"]
    fields: dict[str, SchemaNode] = {}
    for name in ours.keys() | theirs.keys():
        left, right = ours.get(name), theirs.get(name)
        if left is None or right is None:
            present = left if left is not None else right
            assert present is not None  # one side has it, or the name would not be here
            fields[name] = {**present, "optional": True}
        else:
            fields[name] = merge_nodes(left, right)
    return {"type": TYPE_OBJECT, "fields": fields}


def infer_records(payloads: list[dict[str, Any]]) -> SchemaNode:
    """The record schema of a batch (an object node, possibly empty)."""
    node: SchemaNode | None = None
    for payload in payloads:
        node = merge_nodes(node, infer_value(payload))
    return node if node is not None else {"type": TYPE_OBJECT, "fields": {}}


def field_node(schema: SchemaNode, path: str) -> SchemaNode | None:
    """Resolve a dotted path against a record schema; None when absent."""
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
    """Top-level field names, for the "no such field" error."""
    if schema.get("type") != TYPE_OBJECT:
        return []
    return sorted(schema.get("fields", {}))


def render(schema: SchemaNode, depth: int = 0) -> str:
    """The compact human/model-facing form of a schema node."""
    kind = schema.get("type")
    suffix = "?" if schema.get("optional") else ""
    nullable = "|null" if schema.get("nullable") else ""
    if kind == TYPE_OBJECT:
        if depth >= MAX_RENDER_DEPTH:
            return "{…}"
        fields: dict[str, SchemaNode] = schema.get("fields", {})
        names = sorted(fields)
        shown = names[:MAX_RENDERED_FIELDS]
        parts = [
            f"{name}{'?' if fields[name].get('optional') else ''}: "
            f"{render(_plain(fields[name]), depth + 1)}"
            for name in shown
        ]
        if len(names) > len(shown):
            parts.append(f"… +{len(names) - len(shown)} fields")
        return "{" + ", ".join(parts) + "}" + nullable
    if kind == TYPE_ARRAY:
        element = schema.get("element")
        inner = render(element, depth + 1) if element is not None else "?"
        return f"array of {inner}{nullable}{suffix}"
    return f"{kind}{nullable}{suffix}"


def _plain(node: SchemaNode) -> SchemaNode:
    """The node without its `optional` flag (rendered on the key instead)."""
    if node.get("optional"):
        trimmed = dict(node)
        trimmed.pop("optional")
        return trimmed
    return node
