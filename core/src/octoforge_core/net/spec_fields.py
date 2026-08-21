"""Validation of endpoint document fields and parameter declarations."""

import json

from octoforge_core.net.errors import ToolSpecError
from octoforge_core.net.spec_types import ParamKind, ToolParamSpec
from octoforge_core.secrets.api import InvalidSecretError, normalize_host


def load_json(content: str) -> dict[str, object]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ToolSpecError(f"tool spec is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolSpecError("tool spec must be a JSON object")
    return data


def parse_required_text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ToolSpecError(f"{field} must be a non-empty string")
    return raw


def parse_optional_text(raw: object, field: str) -> str | None:
    if raw is None:
        return None
    return parse_required_text(raw, field)


def parse_headers(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolSpecError("headers must be an object of strings")
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ToolSpecError("headers keys must be non-empty strings")
        if not isinstance(value, str):
            raise ToolSpecError(f"headers[{name!r}] must be a string")
    return dict(raw)


def parse_params_schema(raw: object) -> dict[str, ToolParamSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolSpecError("params_schema must be an object")
    return {_parse_param_name(name): _parse_param(str(name), spec) for name, spec in raw.items()}


def _parse_param_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise ToolSpecError("params_schema keys must be non-empty strings")
    if "." in raw:
        raise ToolSpecError(
            f"params_schema key {raw!r} must not contain '.': dotted names are "
            "reserved for the user./secret. namespaces"
        )
    return raw


def _parse_param(name: str, raw: object) -> ToolParamSpec:
    if not isinstance(raw, dict):
        raise ToolSpecError(f"params_schema[{name!r}] must be an object")
    try:
        kind = ParamKind(str(raw.get("type")))
    except ValueError:
        allowed = ", ".join(member.value for member in ParamKind)
        raise ToolSpecError(f"params_schema[{name!r}].type must be one of: {allowed}") from None
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ToolSpecError(f"params_schema[{name!r}].required must be a boolean")
    return ToolParamSpec(required, kind, _parse_hosts(name, raw, kind))


def _parse_hosts(name: str, raw: dict[str, object], kind: ParamKind) -> tuple[str, ...]:
    declared = raw.get("hosts")
    if kind is not ParamKind.HOST:
        if declared is not None:
            raise ToolSpecError(
                f"params_schema[{name!r}]: 'hosts' belongs to a 'host' param, not {kind.value!r}"
            )
        return ()
    if not isinstance(declared, list) or not declared:
        raise ToolSpecError(f"params_schema[{name!r}]: a host param needs a hosts allowlist")
    patterns: list[str] = []
    for item in declared:
        if not isinstance(item, str):
            raise ToolSpecError(f"params_schema[{name!r}].hosts must be strings")
        try:
            patterns.append(normalize_host(item))
        except InvalidSecretError as exc:
            raise ToolSpecError(f"params_schema[{name!r}].hosts: {exc}") from None
    return tuple(patterns)
