"""Parsing of tool instruction records into executable specs.

A tool record's `content` is a JSON document, e.g.:

    {"method": "GET",
     "url_template": "https://wttr.in/{city}?format=j2",
     "params_schema": {"city": {"type": "string", "required": true}},
     "auth": "none"}

Parsing lives on the execution side (core), not in the instructions module:
the module only stores the document.
"""

import json
import string
from dataclasses import dataclass, field
from typing import Any

from octoforge_core.net.errors import ToolSpecError

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
SUPPORTED_PARAM_TYPE = "string"
DEFAULT_AUTH = "none"


@dataclass(frozen=True, slots=True)
class ToolParamSpec:
    """One declared parameter of a tool call."""

    required: bool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Executable view of a tool instruction record.

    `auth` is informational only: internal authorization is injected by the
    executor from the composition-root whitelist, never from the record.
    """

    method: str
    url_template: str
    params: dict[str, ToolParamSpec] = field(default_factory=dict)
    auth: str = DEFAULT_AUTH


def parse_tool_spec(content: str) -> ToolSpec:
    """Parse and validate a tool record's JSON content."""
    data = _load_json(content)
    method = _parse_method(data.get("method"))
    url_template = _parse_url_template(data.get("url_template"))
    params = _parse_params_schema(data.get("params_schema"))
    _validate_template_fields(url_template, params)
    auth = data.get("auth", DEFAULT_AUTH)
    if not isinstance(auth, str):
        raise ToolSpecError("auth must be a string")
    return ToolSpec(method=method, url_template=url_template, params=params, auth=auth)


def _load_json(content: str) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ToolSpecError(f"tool spec is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolSpecError("tool spec must be a JSON object")
    return data


def _parse_method(raw: object) -> str:
    if not isinstance(raw, str):
        raise ToolSpecError("method must be a string")
    method = raw.upper()
    if method not in ALLOWED_METHODS:
        raise ToolSpecError(f"unsupported method: {raw!r}")
    return method


def _parse_url_template(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise ToolSpecError("url_template must be a non-empty string")
    return raw


def _parse_params_schema(raw: object) -> dict[str, ToolParamSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolSpecError("params_schema must be an object")
    params: dict[str, ToolParamSpec] = {}
    for name, spec in raw.items():
        params[_parse_param_name(name)] = _parse_param(name, spec)
    return params


def _parse_param_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise ToolSpecError("params_schema keys must be non-empty strings")
    return raw


def _parse_param(name: str, raw: object) -> ToolParamSpec:
    if not isinstance(raw, dict):
        raise ToolSpecError(f"params_schema[{name!r}] must be an object")
    if raw.get("type") != SUPPORTED_PARAM_TYPE:
        raise ToolSpecError(f"params_schema[{name!r}]: only 'string' params are supported")
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ToolSpecError(f"params_schema[{name!r}].required must be a boolean")
    return ToolParamSpec(required=required)


def _validate_template_fields(url_template: str, params: dict[str, ToolParamSpec]) -> None:
    for _, field_name, _, _ in string.Formatter().parse(url_template):
        if field_name is not None and field_name not in params:
            raise ToolSpecError(f"url_template references undeclared parameter: {field_name!r}")
