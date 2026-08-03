"""Parsing of endpoint instruction records into executable specs.

An endpoint record's `content` is a JSON document, e.g.:

    {"method": "GET",
     "url_template": "https://wttr.in/{city}?format=j2",
     "params_schema": {"city": {"type": "string", "required": true}},
     "auth": "none"}

A record may also declare a request body template and static headers — what
protocols like CalDAV need (an XML `REPORT` body plus a `Depth` header):

    {"method": "REPORT",
     "url_template": "https://cal.example.com/dav/{calendar}/",
     "body_template": "<c:calendar-query ...>{filter}</c:calendar-query>",
     "headers": {"Depth": "1", "Content-Type": "application/xml"},
     ...}

Body values are substituted verbatim (URL-escaping would corrupt an XML or
JSON payload); escaping appropriate to the body's own format is the record
author's concern. Secrets are never substituted into bodies — headers only,
as below.

`auth` may also be an object declaring a per-user secret the executor must
inject as a header at request time:

    "auth": {"secret": "gmail_token",
             "header": "Authorization",
             "format": "Bearer {value}"}

The LLM only ever sees this declaration (the secret *code*); the value is
resolved by the executor from the secret store and never enters any prompt.
The substitution deliberately exists ONLY here, in the admin-authored record
template — never in agent-supplied parameter values, which would hand a
prompt-injected agent an exfiltration channel. Headers only: a query-string
secret would leak into URLs, proxies and logs.

Parsing lives on the execution side (core), not in the instructions module:
the module only stores the document.
"""

import json
import string
from dataclasses import dataclass, field
from typing import Any

from octoforge_core.net.errors import ToolSpecError

# The WebDAV verbs (RFC 4918/3253, CalDAV's MKCALENDAR) are as safe as the
# classic ones: the SSRF guard checks the address, not the method, and write
# verbs (PUT/DELETE) were always allowed. LOCK/UNLOCK are deliberately absent —
# an agent has no way to manage a lock's lifetime and would leave servers with
# dangling locks.
ALLOWED_METHODS = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "PROPFIND",
    "PROPPATCH",
    "REPORT",
    "MKCOL",
    "MKCALENDAR",
    "COPY",
    "MOVE",
)
SUPPORTED_PARAM_TYPE = "string"
DEFAULT_AUTH = "none"
DEFAULT_SECRET_HEADER = "Authorization"
DEFAULT_SECRET_FORMAT = "Bearer {value}"
SECRET_VALUE_FIELD = "{value}"


@dataclass(frozen=True, slots=True)
class ToolParamSpec:
    """One declared parameter of a tool call."""

    required: bool


@dataclass(frozen=True, slots=True)
class SecretAuth:
    """Declaration of the per-user secret a call must carry.

    Resolved by the executor at request time; the record (and therefore the
    LLM) knows the code, never the value.
    """

    code: str
    header: str = DEFAULT_SECRET_HEADER
    format: str = DEFAULT_SECRET_FORMAT


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Executable view of an endpoint instruction record.

    A string `auth` is informational only (legacy records); `secret_auth`
    is the executable declaration parsed from an object-valued `auth`.
    Installation-level authorization still comes from the composition-root
    whitelist, never from the record.
    """

    method: str
    url_template: str
    params: dict[str, ToolParamSpec] = field(default_factory=dict)
    auth: str = DEFAULT_AUTH
    secret_auth: SecretAuth | None = None
    body_template: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def parse_tool_spec(content: str) -> ToolSpec:
    """Parse and validate an endpoint record's JSON content."""
    data = _load_json(content)
    method = _parse_method(data.get("method"))
    url_template = _parse_url_template(data.get("url_template"))
    params = _parse_params_schema(data.get("params_schema"))
    body_template = _parse_body_template(data.get("body_template"))
    _validate_template_fields("url_template", url_template, params)
    if body_template is not None:
        _validate_template_fields("body_template", body_template, params)
    headers = _parse_headers(data.get("headers"))
    raw_auth = data.get("auth", DEFAULT_AUTH)
    if isinstance(raw_auth, str):
        return ToolSpec(
            method=method,
            url_template=url_template,
            params=params,
            auth=raw_auth,
            body_template=body_template,
            headers=headers,
        )
    if isinstance(raw_auth, dict):
        return ToolSpec(
            method=method,
            url_template=url_template,
            params=params,
            auth="secret",
            secret_auth=_parse_secret_auth(raw_auth),
            body_template=body_template,
            headers=headers,
        )
    raise ToolSpecError("auth must be a string or a secret declaration object")


def _parse_secret_auth(raw: dict[str, object]) -> SecretAuth:
    code = raw.get("secret")
    if not isinstance(code, str) or not code.strip():
        raise ToolSpecError("auth.secret must be a non-empty secret code")
    header = raw.get("header", DEFAULT_SECRET_HEADER)
    if not isinstance(header, str) or not header.strip():
        raise ToolSpecError("auth.header must be a non-empty header name")
    value_format = raw.get("format", DEFAULT_SECRET_FORMAT)
    if not isinstance(value_format, str) or SECRET_VALUE_FIELD not in value_format:
        raise ToolSpecError(f"auth.format must contain the {SECRET_VALUE_FIELD} placeholder")
    return SecretAuth(code=code.strip(), header=header.strip(), format=value_format)


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


def _parse_body_template(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ToolSpecError("body_template must be a non-empty string")
    return raw


def _parse_headers(raw: object) -> dict[str, str]:
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


def _validate_template_fields(label: str, template: str, params: dict[str, ToolParamSpec]) -> None:
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is None:
            continue
        param = params.get(field_name)
        if param is None:
            raise ToolSpecError(f"{label} references undeclared parameter: {field_name!r}")
        if not param.required:
            # an optional template field would crash the render with a raw
            # KeyError the moment a caller omits it
            raise ToolSpecError(f"{label} parameter must be required: {field_name!r}")
