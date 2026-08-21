"""Infer collection schemas by folding JSON-shaped records."""

from typing import Any

TYPE_OBJECT = "object"
TYPE_ARRAY = "array"
TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_NULL = "null"

SchemaNode = dict[str, Any]


def infer_value(value: object) -> SchemaNode:
    """Return the schema node for one JSON-shaped value."""
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
    return {"type": TYPE_STRING}


def merge_nodes(current: SchemaNode | None, incoming: SchemaNode) -> SchemaNode:
    """Fold one observation into a schema, retaining optional/null flags."""
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
            assert present is not None
            fields[name] = {**present, "optional": True}
        else:
            fields[name] = merge_nodes(left, right)
    return {"type": TYPE_OBJECT, "fields": fields}


def infer_records(payloads: list[dict[str, Any]]) -> SchemaNode:
    """Return the record schema of a batch, including an empty batch."""
    node: SchemaNode | None = None
    for payload in payloads:
        node = merge_nodes(node, infer_value(payload))
    return node if node is not None else {"type": TYPE_OBJECT, "fields": {}}
