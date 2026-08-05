"""Parsing of endpoint instruction records into executable specs.

An endpoint record's `content` is a JSON document, e.g.:

    {"method": "GET",
     "url_template": "https://wttr.in/{city}?format=j2",
     "params_schema": {"city": {"type": "string", "required": true}},
     "auth": "none"}

A record may also declare a request body template and headers — what
protocols like CalDAV need (an XML `REPORT` body plus a `Depth` header):

    {"method": "REPORT",
     "url_template": "https://cal.example.com/dav/{calendar}/",
     "body_template": "<c:calendar-query ...>{filter}</c:calendar-query>",
     "headers": {"Depth": "1", "Content-Type": "application/xml"},
     ...}

Every template part — `url_template`, `body_template` and each header value —
speaks one placeholder language with three namespaces:

    {city}          a model-supplied parameter, declared in params_schema
    {user.timezone} a per-user stored value, injected by the executor
    {secret.token}  a per-user secret, resolved and injected by the executor

The LLM sees the record (and therefore every placeholder NAME); the values of
the `user.` and `secret.` namespaces are substituted server-side after the
model's output is fixed and never enter any prompt. Substitution exists ONLY
in the record-authored templates — never in agent-supplied parameter values,
which would hand a prompt-injected agent an exfiltration channel. Where a
secret may land is the secret's own `placements` setting (headers by
default); a secret can never form the URL host, and body values go in
verbatim (escaping appropriate to the body's format is the record author's
concern).

`auth` remains as sugar for the common one-header case and expands into a
header template:

    "auth": {"secret": "gmail_token",
             "header": "Authorization",
             "format": "Bearer {value}"}

is exactly `"headers": {"Authorization": "Bearer {secret.gmail_token}"}`.

Parsing lives on the execution side (core), not in the instructions module:
the module only stores the document.
"""

import json
import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from octoforge_core.net.errors import ToolSpecError

# Host patterns are the secrets module's grammar (`api.example.com` or
# `*.example.com`), reused rather than reinvented: a record author who has
# bound a secret to a host already knows it.
from octoforge_core.secrets.api import InvalidSecretError, normalize_host

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
DEFAULT_AUTH = "none"
DEFAULT_SECRET_HEADER = "Authorization"
DEFAULT_SECRET_FORMAT = "Bearer {value}"
SECRET_VALUE_FIELD = "{value}"
USER_NAMESPACE = "user"
SECRET_NAMESPACE = "secret"
# Fields this parser implements. Anything else is refused: an ignored field
# is how a record ends up sending no credential and no body (see
# `_reject_unknown_keys`).
SPEC_KEYS = frozenset(
    {"method", "url_template", "params_schema", "body_template", "headers", "auth"}
)
# Free-form documentation the author may keep next to the contract. Ignored
# by execution, allowed by name so a note never has to look like a feature.
ANNOTATION_KEYS = frozenset({"notes", "description"})
KNOWN_KEYS = SPEC_KEYS | ANNOTATION_KEYS
# same grammar as the secrets and params modules' codes
REF_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


class ParamKind(StrEnum):
    """What a declared parameter may carry, and how it is escaped.

    A narrow vocabulary rather than one free-form URL: the model fills
    obvious slots, and each slot has its own check. The record always writes
    the scheme, so no parameter can choose the protocol.
    """

    # one path segment: every reserved character is escaped, slashes included
    STRING = "string"
    # several path segments: each is escaped, the slashes between them survive.
    # Discovery protocols (CalDAV, CardDAV) hand back paths, and a `string`
    # turns `a/b/` into `a%2Fb%2F`, which the far side answers 401/404 to.
    PATH = "path"
    # a hostname, and only one the record's `hosts` allowlist covers. This is
    # what lets a contract follow a service that shards across sibling hosts
    # (iCloud answers on p54-caldav.icloud.com after discovery) without
    # letting the model name the destination itself.
    HOST = "host"


@dataclass(frozen=True, slots=True)
class ToolParamSpec:
    """One declared parameter of a tool call."""

    required: bool
    kind: ParamKind = ParamKind.STRING
    # for HOST params: the patterns the value must match (exact hostnames or
    # `*.example.com`, the same grammar a secret's binding uses)
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateRefs:
    """The placeholder names one template part references, by namespace."""

    params: frozenset[str] = frozenset()
    user_params: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()

    def __or__(self, other: "TemplateRefs") -> "TemplateRefs":
        return TemplateRefs(
            params=self.params | other.params,
            user_params=self.user_params | other.user_params,
            secrets=self.secrets | other.secrets,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Executable view of an endpoint instruction record.

    A string `auth` is informational only (legacy records); an object-valued
    `auth` has already been expanded into a header template by the parser.
    Installation-level authorization still comes from the composition-root
    whitelist, never from the record.
    """

    method: str
    url_template: str
    params: dict[str, ToolParamSpec] = field(default_factory=dict)
    auth: str = DEFAULT_AUTH
    body_template: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def collect_refs(template: str) -> TemplateRefs:
    """Collect and validate the placeholder names of one template part.

    Rejects what the renderer will not do: empty fields, format specs and
    conversions (`{x:>10}`, `{x!r}` — substitution here is verbatim by
    design), unknown namespaces and malformed `user.`/`secret.` codes.
    """
    params: set[str] = set()
    user_params: set[str] = set()
    secrets: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ToolSpecError(f"malformed template: {exc}") from exc
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name:
            raise ToolSpecError("empty placeholder {} is not allowed in templates")
        if format_spec or conversion:
            raise ToolSpecError(
                f"placeholder {{{field_name}}} carries a format spec or conversion; "
                "substitution is verbatim — remove it"
            )
        if "." not in field_name:
            params.add(field_name)
            continue
        namespace, _, code = field_name.partition(".")
        if namespace == USER_NAMESPACE:
            _validate_ref_code(field_name, code)
            user_params.add(code)
        elif namespace == SECRET_NAMESPACE:
            _validate_ref_code(field_name, code)
            secrets.add(code)
        else:
            raise ToolSpecError(
                f"unknown namespace in placeholder {{{field_name}}}: "
                f"only '{USER_NAMESPACE}.' and '{SECRET_NAMESPACE}.' exist"
            )
    return TemplateRefs(
        params=frozenset(params),
        user_params=frozenset(user_params),
        secrets=frozenset(secrets),
    )


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Substitute values into a template part, verbatim.

    Keys are full field names (`city`, `user.timezone`, `secret.token`);
    escaping appropriate to the destination (URL quoting, header safety)
    is the caller's concern, applied to the values before rendering.
    Every referenced field must be present: `collect_refs` + the callers'
    validation guarantee that, so a KeyError here is a programming error.
    """
    parts: list[str] = []
    for literal, field_name, _, _ in string.Formatter().parse(template):
        parts.append(literal)
        if field_name is not None:
            parts.append(values[field_name])
    return "".join(parts)


def _validate_ref_code(field_name: str, code: str) -> None:
    if not REF_CODE_PATTERN.match(code):
        raise ToolSpecError(
            f"placeholder {{{field_name}}} must reference a code of 1-64 characters of [a-z0-9_]"
        )


def parse_tool_spec(content: str) -> ToolSpec:
    """Parse and validate an endpoint record's JSON content."""
    data = _load_json(content)
    _reject_unknown_keys(data)
    method = _parse_method(data.get("method"))
    url_template = _parse_url_template(data.get("url_template"))
    params = _parse_params_schema(data.get("params_schema"))
    body_template = _parse_body_template(data.get("body_template"))
    headers = _parse_headers(data.get("headers"))
    raw_auth = data.get("auth", DEFAULT_AUTH)
    if isinstance(raw_auth, dict):
        auth = "secret"
        header_name, header_template = _expand_secret_auth(raw_auth)
        # the sugar wins a name collision with a static record header: the
        # record author wrote both, and the auth block is the specific one
        headers[header_name] = header_template
    elif isinstance(raw_auth, str):
        auth = _parse_auth_word(raw_auth)
    else:
        raise ToolSpecError("auth must be a string or a secret declaration object")
    _validate_template_fields("url_template", url_template, params)
    _validate_url_authority(url_template, params)
    if body_template is not None:
        _validate_template_fields("body_template", body_template, params)
    for name, value in headers.items():
        _validate_template_fields(f"headers[{name!r}]", value, params)
    return ToolSpec(
        method=method,
        url_template=url_template,
        params=params,
        auth=auth,
        body_template=body_template,
        headers=headers,
    )


def _reject_unknown_keys(data: dict[str, Any]) -> None:
    """Refuse a document with fields this parser does not implement.

    Silence here is how a call ends up with no credential and no body: a
    record author (often the model itself) invents `secret_key` or `body`,
    every invented field is ignored, and the only symptom is a bare 401 from
    the far side. Naming the allowed keys turns that into a one-step fix.
    """
    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        raise ToolSpecError(
            f"unknown field(s) in the endpoint document: {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(KNOWN_KEYS))}. A secret is declared as "
            'auth: {"secret": "<code>"} or referenced as {secret.<code>} inside a '
            "header/url/body template; the request body is body_template"
        )


def _parse_auth_word(raw: str) -> str:
    """Validate a string-valued `auth`: only "none" says nothing.

    Any other word ("basic", "bearer") reads as a declaration that the call
    is authenticated, while a string `auth` injects exactly nothing — the
    request goes out bare and the far side answers 401.
    """
    auth = raw.strip().lower()
    if auth != DEFAULT_AUTH:
        raise ToolSpecError(
            f"auth: {raw!r} declares an authentication scheme but attaches no "
            'credential. Use auth: {"secret": "<code>", "format": "Basic {value}"} '
            'for a per-user secret, or auth: "none" for a public endpoint'
        )
    return auth


def _expand_secret_auth(raw: dict[str, object]) -> tuple[str, str]:
    """The `auth` object as the header template it is sugar for."""
    code = raw.get("secret")
    if not isinstance(code, str) or not code.strip():
        raise ToolSpecError("auth.secret must be a non-empty secret code")
    code = code.strip().lower()
    if not REF_CODE_PATTERN.match(code):
        raise ToolSpecError("auth.secret must be 1-64 characters of [a-z0-9_]")
    header = raw.get("header", DEFAULT_SECRET_HEADER)
    if not isinstance(header, str) or not header.strip():
        raise ToolSpecError("auth.header must be a non-empty header name")
    value_format = raw.get("format", DEFAULT_SECRET_FORMAT)
    if not isinstance(value_format, str) or SECRET_VALUE_FIELD not in value_format:
        raise ToolSpecError(f"auth.format must contain the {SECRET_VALUE_FIELD} placeholder")
    template = value_format.replace(SECRET_VALUE_FIELD, f"{{{SECRET_NAMESPACE}.{code}}}")
    return header.strip(), template


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
    return ToolParamSpec(required=required, kind=kind, hosts=_parse_param_hosts(name, raw, kind))


def _parse_param_hosts(name: str, raw: dict[str, object], kind: ParamKind) -> tuple[str, ...]:
    """The allowlist a `host` param is confined to; nothing else takes one."""
    declared = raw.get("hosts")
    if kind is not ParamKind.HOST:
        if declared is not None:
            raise ToolSpecError(
                f"params_schema[{name!r}]: 'hosts' belongs to a 'host' param, not {kind.value!r}"
            )
        return ()
    if not isinstance(declared, list) or not declared:
        raise ToolSpecError(
            f"params_schema[{name!r}]: a 'host' param must declare a non-empty 'hosts' "
            'allowlist, e.g. ["*.icloud.com"] — the record names where it may go, '
            "never the caller"
        )
    patterns: list[str] = []
    for item in declared:
        if not isinstance(item, str):
            raise ToolSpecError(f"params_schema[{name!r}].hosts must be strings")
        try:
            # the same grammar a secret's binding uses, so an author learns it once
            patterns.append(normalize_host(item))
        except InvalidSecretError as exc:
            raise ToolSpecError(f"params_schema[{name!r}].hosts: {exc}") from None
    return tuple(patterns)


def _validate_template_fields(label: str, template: str, params: dict[str, ToolParamSpec]) -> None:
    try:
        refs = collect_refs(template)
    except ToolSpecError as exc:
        raise ToolSpecError(f"{label}: {exc}") from None
    for field_name in sorted(refs.params):
        param = params.get(field_name)
        if param is None:
            raise ToolSpecError(f"{label} references undeclared parameter: {field_name!r}")
        if not param.required:
            # an optional template field would fail the render the moment a
            # caller omits it
            raise ToolSpecError(f"{label} parameter must be required: {field_name!r}")


def _validate_url_authority(url_template: str, params: dict[str, ToolParamSpec]) -> None:
    """Police what may appear in the URL's scheme and host.

    That prefix decides where the request goes, and it is what the SSRF
    guard and a secret's host binding judge. So:

    - a secret may never appear there — the binding check would be judging a
      value the secret itself produced;
    - a model-supplied parameter may appear there only as a `host` param,
      whose allowlist the RECORD wrote. A plain `string`/`path` there would
      let the caller name the destination outright.

    A `{user.*}` value is allowed: an operator set it, and an operator can
    write any URL into the record anyway.
    """
    scheme_end = url_template.find("://")
    path_start = url_template.find("/", scheme_end + 3 if scheme_end >= 0 else 0)
    prefix = url_template if path_start < 0 else url_template[:path_start]
    refs = collect_refs(prefix)
    if refs.secrets:
        raise ToolSpecError("a secret placeholder cannot appear in the URL scheme or host")
    for field_name in sorted(refs.params):
        param = params.get(field_name)
        if param is not None and param.kind is not ParamKind.HOST:
            raise ToolSpecError(
                f"url_template puts {field_name!r} in the host, but it is a "
                f"{param.kind.value!r} param: only a 'host' param (with its own "
                "'hosts' allowlist) may decide where the request goes"
            )
