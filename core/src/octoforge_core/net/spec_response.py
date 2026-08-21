"""Parsing of response-shaping and pagination endpoint sections."""

from octoforge_core.net.errors import ToolSpecError
from octoforge_core.net.spec_types import (
    FieldCoercion,
    PaginationKind,
    PaginationSpec,
    ResponseSpec,
    ToolParamSpec,
)


def parse_response(raw: object) -> ResponseSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ToolSpecError("response must be an object: {items_path?, fields?}")
    unknown = sorted(set(raw) - {"items_path", "fields"})
    if unknown:
        raise ToolSpecError(f"unknown field(s) in response: {', '.join(unknown)}")
    items_path = raw.get("items_path")
    if items_path is not None and (not isinstance(items_path, str) or not items_path.strip()):
        raise ToolSpecError("response.items_path must be a non-empty dotted path")
    return ResponseSpec(items_path=items_path, fields=_parse_fields(raw.get("fields")))


def parse_pagination(
    raw: object,
    params: dict[str, ToolParamSpec],
) -> PaginationSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ToolSpecError("pagination must be an object: {kind, param, ...}")
    kind = _pagination_kind(raw.get("kind"))
    param = raw.get("param")
    if not isinstance(param, str) or param not in params:
        raise ToolSpecError(
            "pagination.param must name a declared parameter (the collect loop advances it)"
        )
    start = raw.get("start", 1 if kind is PaginationKind.PAGE else 0)
    if not isinstance(start, int) or isinstance(start, bool):
        raise ToolSpecError("pagination.start must be an integer")
    cursor_path = raw.get("cursor_path")
    if kind is PaginationKind.CURSOR and not isinstance(cursor_path, str):
        raise ToolSpecError("pagination.cursor_path is required for kind 'cursor'")
    total_path = raw.get("total_path")
    if total_path is not None and not isinstance(total_path, str):
        raise ToolSpecError("pagination.total_path must be a dotted path string")
    _reject_pagination_unknown(raw)
    return PaginationSpec(
        kind,
        param,
        start,
        cursor_path if isinstance(cursor_path, str) else None,
        total_path,
    )


def _parse_fields(raw: object) -> dict[str, FieldCoercion]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolSpecError(
            'response.fields must map field names to types, e.g. {"price": "number"}'
        )
    fields: dict[str, FieldCoercion] = {}
    for name, value in raw.items():
        try:
            fields[str(name)] = FieldCoercion(str(value))
        except ValueError as exc:
            allowed = ", ".join(sorted(member.value for member in FieldCoercion))
            raise ToolSpecError(
                f"response.fields[{name!r}]: unknown type {value!r}; allowed: {allowed}"
            ) from exc
    return fields


def _pagination_kind(raw: object) -> PaginationKind:
    try:
        return PaginationKind(str(raw))
    except ValueError as exc:
        allowed = ", ".join(member.value for member in PaginationKind)
        raise ToolSpecError(f"pagination.kind must be one of: {allowed}") from exc


def _reject_pagination_unknown(raw: dict[object, object]) -> None:
    known = {"kind", "param", "start", "cursor_path", "total_path"}
    unknown = sorted(str(name) for name in set(raw) - known)
    if unknown:
        raise ToolSpecError(f"unknown field(s) in pagination: {', '.join(unknown)}")
