"""Schema inference and endpoint-declared record shaping."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from octoforge_core.net.collections.api import NewRecords
from octoforge_core.net.collections.schema_infer import infer_records, merge_nodes
from octoforge_core.net.tool_spec import FieldCoercion

INFER_IN_THREAD_RECORDS = 2000


async def infer_schema(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive a schema without making a large batch block the event loop."""
    if len(payloads) > INFER_IN_THREAD_RECORDS:
        return await asyncio.to_thread(infer_records, payloads)
    return infer_records(payloads)


async def merge_schema(
    current: dict[str, Any] | None, payloads: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fold a batch into the running schema, offloading large batches."""
    if len(payloads) > INFER_IN_THREAD_RECORDS:
        return await asyncio.to_thread(lambda: merge_nodes(current, infer_records(payloads)))
    return merge_nodes(current, infer_records(payloads))


def shape_records(records: NewRecords, fields: Mapping[str, FieldCoercion]) -> NewRecords:
    """Apply an endpoint record's declared projection and coercions."""
    if not fields:
        return records
    shaped = [
        {name: _coerce(payload[name], kind) for name, kind in fields.items() if name in payload}
        for payload in records.payloads
    ]
    return NewRecords(payloads=shaped, source=records.source)


def _coerce(value: object, kind: FieldCoercion) -> object:
    if kind is FieldCoercion.NUMBER and isinstance(value, str):
        return _coerce_number(value)
    if kind is FieldCoercion.STRING and value is not None and not isinstance(value, str):
        if isinstance(value, dict | list):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    if kind is FieldCoercion.BOOLEAN and isinstance(value, str):
        return _coerce_boolean(value)
    return value


def _coerce_number(value: str) -> object:
    try:
        number = float(value.strip())
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _coerce_boolean(value: str) -> object:
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    return value
