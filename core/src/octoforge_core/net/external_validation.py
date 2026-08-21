"""Validation of model-supplied classic endpoint parameters."""

from typing import Any

from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.spec_types import ParamKind, ToolParamSpec, ToolSpec
from octoforge_core.secrets.api import host_matches


def validate_params(spec: ToolSpec, params: dict[str, Any]) -> dict[str, str]:
    unknown = sorted(set(params) - set(spec.params))
    if unknown:
        raise ExternalCallError(f"unknown params: {', '.join(unknown)}")
    missing = sorted(
        name for name, param in spec.params.items() if param.required and name not in params
    )
    if missing:
        raise ExternalCallError(f"missing required params: {', '.join(missing)}")
    not_strings = sorted(name for name, value in params.items() if not isinstance(value, str))
    if not_strings:
        raise ExternalCallError(
            f"params must be strings for this endpoint: {', '.join(not_strings)}"
        )
    return {name: _validate_value(name, spec.params[name], value) for name, value in params.items()}


def _validate_value(name: str, spec: ToolParamSpec, value: str) -> str:
    match spec.kind:
        case ParamKind.STRING:
            return value
        case ParamKind.PATH:
            return _validate_path(name, value)
        case ParamKind.HOST:
            return _validate_host(name, spec, value)


def _validate_path(name: str, value: str) -> str:
    path = value.strip().lstrip("/")
    if any(char in path for char in "?#") or "://" in path:
        raise ExternalCallError(f"param {name!r} is a path, not a URL")
    if any(segment == ".." for segment in path.split("/")):
        raise ExternalCallError(f"param {name!r} must not walk up the path with '..'")
    return path


def _validate_host(name: str, spec: ToolParamSpec, value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not host or any(char in host for char in "/@:?#") or "*" in host:
        raise ExternalCallError(f"param {name!r} must be a bare hostname")
    if not any(host_matches(pattern, host) for pattern in spec.hosts):
        raise ExternalCallError(
            f"param {name!r}: host {host!r} is not one this endpoint may call "
            f"(allowed: {', '.join(spec.hosts)})"
        )
    return host
