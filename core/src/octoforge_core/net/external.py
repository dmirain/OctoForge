"""Executor of external calls described by endpoint instruction records.

Core-side execution: the instructions module only stores/searches/ranks
endpoint records; this executor reads them through the `InstructionService`
facade, validates and renders the templates, applies the SSRF guard and the
composition-root auth whitelist, and performs the HTTP request.

Substitution order is the security order:

1. model params are validated, then rendered together with the user's stored
   `{user.*}` values — the URL that results is what the SSRF guard and the
   host binding judge;
2. secrets are resolved only after those checks, for the host the URL
   actually names, and only into the request parts the secret's own
   `placements` allow; in the URL they ride as unguessable sentinels until
   the checks have passed;
3. whatever comes back is scrubbed of every resolved secret — both the form
   that was sent and the stored plain value — before the model or the
   archive sees it.

An endpoint record's content may declare a `kind`, marking a contract some
other protocol executes (today: MCP tool mirrors). The executor only sniffs
the discriminator and hands the record to the delegate registered for that
kind — it knows nothing about the protocols behind them, which keeps the
dependency arrow pointing at this module.
"""

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from octoforge_core.config import DEFAULT_TIMEOUT_SECONDS
from octoforge_core.instructions.api import InstructionService, InstructionType
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.guard import SsrfGuard, matches_url_prefix
from octoforge_core.net.tool_spec import (
    TemplateRefs,
    ToolSpec,
    collect_refs,
    parse_tool_spec,
    render_template,
)
from octoforge_core.params.api import UserParamStore
from octoforge_core.secrets.api import (
    InvalidSecretError,
    ResolvedSecret,
    SecretHostMismatchError,
    SecretNotFoundError,
    SecretPlacement,
    SecretStore,
)

MAX_BODY_CHARS = 8000
# Byte ceiling for what is read off the wire before the character cap applies:
# an endpoint answering with gigabytes must not be buffered whole.
MAX_BODY_BYTES = 2 * 1024 * 1024
TRUNCATED_SUFFIX = "\n...[truncated]"
USER_ID_PLACEHOLDER = "{user_id}"
SECRET_SCRUBBED = "[secret]"
SECRETS_DISABLED_MESSAGE = (
    "this endpoint requires a per-user secret, but secrets are not configured "
    "on this installation (OF_SECRETS_KEY is not set)"
)
PARAMS_DISABLED_MESSAGE = (
    "this endpoint uses per-user params ({{user.*}}), but user params are not "
    "wired on this installation"
)
SECRET_MISSING_TEMPLATE = (
    "secret '{code}' is not set for this user (needed for host '{host}'): mint "
    "them a pre-filled one-time form link with the secret_link tool — pass this "
    "code, this host and a clear description of what the secret is for. Without "
    "the tool they can run /secrets in Telegram and fill the form by hand"
)
PARAM_MISSING_TEMPLATE = (
    "user param(s) not set for this user: {codes}. An operator sets them in the "
    "admin console; tell the user which value is needed and what for"
)
PLACEMENT_BLOCKED_TEMPLATE = (
    "secret '{code}' may not be substituted into the {part} of a request "
    "(it allows: {allowed}); the user can extend its placements in the secrets form"
)
UNAUTHENTICATED_STATUSES = frozenset({401, 403})
NO_CREDENTIAL_HINT = (
    "\n\n[octoforge] This request carried NO credential: the endpoint record "
    "references no secret, so nothing was attached. Do not guess at the secret's "
    "value or encoding — fix the record. Declare the secret as "
    'auth: {"secret": "<code>"} or reference it as {secret.<code>} in a header '
    "template, and check secret_list for the codes this user actually has."
)


async def read_capped_text(response: httpx.Response, limit: int) -> tuple[str, bool]:
    """Read at most `limit` bytes of a streaming response and decode them.

    Returns the text and whether the body was cut short. Decoding uses the
    response's own charset with replacement, so a cut in the middle of a
    multi-byte character degrades to a replacement glyph instead of an error.
    """
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            truncated = True
            break
    raw = b"".join(chunks)[:limit]
    return raw.decode(response.encoding or "utf-8", errors="replace"), truncated


@dataclass(frozen=True, slots=True)
class ExternalCallAuth:
    """Internal authorization injected for an allowlisted origin.

    The base-url prefix is compared by parsed origin (scheme/host/port, the
    path ignored), so userinfo-spoofed URLs never receive the header.
    `header_value` may contain the `{user_id}` placeholder, substituted with
    the calling user's id at execution time; a call without a user id then
    sends no header at all.
    """

    base_url_prefix: str
    header_name: str
    header_value: str


@dataclass(frozen=True, slots=True)
class CallCredentials:
    """Credential and per-user value sources of the executor.

    `auth_whitelist` — installation-level headers for allowlisted origins
    (from the composition root's env). `secrets` — the per-user secret store
    for `{secret.*}` templates; None disables the feature. `user_params` —
    the per-user non-secret values `{user.*}` templates reference; None
    disables that feature.
    """

    auth_whitelist: tuple[ExternalCallAuth, ...] = ()
    secrets: SecretStore | None = None
    user_params: UserParamStore | None = None


@dataclass(frozen=True, slots=True)
class ExternalCallResult:
    """Outcome of an external call; error statuses are data, not exceptions.

    `status` 0 means the transport carried no HTTP status worth showing
    (a kind delegate that is not plain HTTP); the tool then renders the
    body alone.
    """

    status: int
    body: str


class KindCallDelegate(Protocol):
    """Executes endpoint records of one content `kind` (e.g. MCP mirrors)."""

    async def execute(
        self, content: str, params: dict[str, Any], user_id: str | None
    ) -> ExternalCallResult:
        """Run the call the record's content describes, params as given."""
        ...


@dataclass(frozen=True, slots=True)
class _RenderPlan:
    """Namespaced references of every template part of one call."""

    url_refs: TemplateRefs
    body_refs: TemplateRefs
    header_refs: dict[str, TemplateRefs]

    @property
    def combined(self) -> TemplateRefs:
        refs = self.url_refs | self.body_refs
        for header in self.header_refs.values():
            refs = refs | header
        return refs


class ExternalCallExecutor:
    """Executes endpoint records fetched through the instructions facade."""

    def __init__(  # noqa: PLR0913, PLR0917 — the executor's full credential/delegate surface
        self,
        service: InstructionService,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        credentials: CallCredentials | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        delegates: Mapping[str, KindCallDelegate] | None = None,
    ) -> None:
        resolved = credentials if credentials is not None else CallCredentials()
        self._service = service
        self._http = http_client
        self._guard = guard
        self._auth_whitelist = resolved.auth_whitelist
        self._timeout = timeout_seconds
        self._secrets = resolved.secrets
        self._user_params = resolved.user_params
        self._delegates = dict(delegates or {})

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        user_id: str | None = None,
    ) -> ExternalCallResult:
        """Run the endpoint call `name` with validated params and return status + body.

        Only endpoints visible to `user_id` resolve (the caller's own private
        records plus public ones); without a user id only public endpoints do.
        """
        instruction = await self._service.get_by_name(
            name, InstructionType.ENDPOINT, user_id=user_id
        )
        kind = _content_kind(instruction.content)
        if kind is not None:
            delegate = self._delegates.get(kind)
            if delegate is None:
                raise ExternalCallError(
                    f"endpoint '{name}' declares kind {kind!r}, which this "
                    "installation has no executor for"
                )
            return await delegate.execute(instruction.content, params, user_id)
        spec = parse_tool_spec(instruction.content)
        try:
            validated = _validate_params(spec, params)
        except ExternalCallError as exc:
            # a blind call (endpoint_get skipped): return the declared contract
            # with the error so the model self-corrects in one step instead of
            # retrying guessed parameter variations
            raise ExternalCallError(
                f"{exc}; the endpoint declares this contract: {instruction.content}"
            ) from exc
        plan = _collect_plan(spec)
        user_values = await self._user_values(plan, user_id)
        # secrets ride the URL as unguessable sentinels until the guard and
        # the host binding have judged the real destination
        sentinels = {code: f"of-secret-{uuid.uuid4().hex}" for code in plan.url_refs.secrets}
        safe_url = _render_url(spec, validated, user_values, sentinels)
        await self._guard.check(safe_url)
        secrets = await self._resolve_secrets(plan, safe_url, user_id)
        url = _substitute_url_secrets(safe_url, sentinels, secrets)
        render_values: dict[str, str] = {
            **validated,
            **user_values,
            **{f"secret.{code}": resolved.value for code, resolved in secrets.items()},
        }
        headers = self._build_headers(spec, plan, url, user_id, render_values)
        body = (
            render_template(spec.body_template, render_values)
            if spec.body_template is not None
            else None
        )
        try:
            async with self._http.stream(
                spec.method,
                url,
                headers=headers,
                content=body,
                follow_redirects=False,  # a redirect would bypass the guard's URL check
                timeout=self._timeout,
            ) as response:
                raw, truncated = await read_capped_text(response, MAX_BODY_BYTES)
                status = response.status_code
        except httpx.HTTPError as exc:
            # the exception text can carry the request URL, secrets included
            raise ExternalCallError(
                f"external call failed: {_scrub(str(exc), secrets.values())}"
            ) from exc
        result = _truncate(_scrub(raw, secrets.values()))
        if truncated and not result.endswith(TRUNCATED_SUFFIX):
            result += TRUNCATED_SUFFIX
        if status in UNAUTHENTICATED_STATUSES and not secrets:
            # a bare 401/403 reads as "wrong credential" and sends the model
            # guessing at the value; when the record attached none at all,
            # say so — that is a one-step fix in the record, not a mystery
            result += NO_CREDENTIAL_HINT
        return ExternalCallResult(status=status, body=result)

    async def _user_values(self, plan: _RenderPlan, user_id: str | None) -> dict[str, str]:
        """The `{user.*}` values of this call, keyed by full field name."""
        codes = plan.combined.user_params
        if not codes:
            return {}
        if self._user_params is None:
            raise ExternalCallError(PARAMS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError("this endpoint uses per-user params: no user in context")
        stored = await self._user_params.get_for_user(user_id)
        missing = sorted(codes - stored.keys())
        if missing:
            raise ExternalCallError(
                PARAM_MISSING_TEMPLATE.format(codes=", ".join(f"'{code}'" for code in missing))
            )
        return {f"user.{code}": stored[code] for code in codes}

    async def _resolve_secrets(
        self, plan: _RenderPlan, url: str, user_id: str | None
    ) -> dict[str, ResolvedSecret]:
        """Resolve every referenced secret for the target host, placements enforced.

        The single moment secret values exist outside the store: they go into
        the request parts their placements allow and are never logged, never
        returned to the caller and scrubbed from the response body. Failures
        translate into agent-readable guidance (the code, never the value).
        """
        codes = plan.combined.secrets
        if not codes:
            return {}
        if self._secrets is None:
            raise ExternalCallError(SECRETS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError("this endpoint requires a per-user secret: no user in context")
        host = (urlsplit(url).hostname or "").lower()
        resolved: dict[str, ResolvedSecret] = {}
        for code in sorted(codes):
            try:
                resolved[code] = await self._secrets.resolve(user_id, code, host)
            except SecretNotFoundError:
                raise ExternalCallError(
                    SECRET_MISSING_TEMPLATE.format(code=code, host=host)
                ) from None
            except (SecretHostMismatchError, InvalidSecretError) as exc:
                raise ExternalCallError(str(exc)) from None
        _enforce_placements(plan, resolved)
        return resolved

    def _build_headers(
        self,
        spec: ToolSpec,
        plan: _RenderPlan,
        url: str,
        user_id: str | None,
        render_values: Mapping[str, str],
    ) -> dict[str, str]:
        """Render header templates and merge the credential sources.

        Precedence on a name collision, lowest first: record headers without
        secret refs, the installation whitelist, record headers WITH secret
        refs — so a plain record header can never shadow a credential, and
        the record's own secret-bearing header stays authoritative for the
        endpoint it was written for.
        """
        plain: dict[str, str] = {}
        secret_bearing: dict[str, str] = {}
        for name, template in spec.headers.items():
            rendered = render_template(template, render_values)
            if not _is_header_safe(rendered):
                # the value never goes into the error: it may embed a secret
                raise ExternalCallError(
                    f"header {name!r} renders to an illegal value (control characters or non-ASCII)"
                )
            target = secret_bearing if plan.header_refs[name].secrets else plain
            target[name] = rendered
        headers = plain
        headers.update(self._auth_headers_for(url, user_id))
        headers.update(secret_bearing)
        return headers

    def _auth_headers_for(self, url: str, user_id: str | None) -> dict[str, str]:
        for entry in self._auth_whitelist:
            if matches_url_prefix(url, entry.base_url_prefix):
                return _render_auth_header(entry, user_id)
        return {}


def _render_auth_header(entry: ExternalCallAuth, user_id: str | None) -> dict[str, str]:
    if USER_ID_PLACEHOLDER not in entry.header_value:
        return {entry.header_name: entry.header_value}
    if user_id is None:
        return {}
    return {entry.header_name: entry.header_value.replace(USER_ID_PLACEHOLDER, user_id)}


def _content_kind(content: str) -> str | None:
    """The record's `kind` discriminator; None for classic endpoint contracts."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None  # not even JSON: let parse_tool_spec produce its own error
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    return kind if isinstance(kind, str) and kind else None


def _validate_params(spec: ToolSpec, params: dict[str, Any]) -> dict[str, str]:
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
        # structured values belong to kind-delegated records (MCP); a classic
        # endpoint renders params into a URL and takes strings only
        joined = ", ".join(not_strings)
        raise ExternalCallError(f"params must be strings for this endpoint: {joined}")
    return dict(params)


def _collect_plan(spec: ToolSpec) -> _RenderPlan:
    """Re-collect the namespaced refs; parse-time validation guarantees success."""
    return _RenderPlan(
        url_refs=collect_refs(spec.url_template),
        body_refs=(
            collect_refs(spec.body_template) if spec.body_template is not None else TemplateRefs()
        ),
        header_refs={name: collect_refs(value) for name, value in spec.headers.items()},
    )


def _render_url(
    spec: ToolSpec,
    params: dict[str, str],
    user_values: Mapping[str, str],
    sentinels: Mapping[str, str],
) -> str:
    values = {name: quote(value, safe="") for name, value in params.items()}
    values.update({name: quote(value, safe="") for name, value in user_values.items()})
    values.update({f"secret.{code}": sentinel for code, sentinel in sentinels.items()})
    return render_template(spec.url_template, values)


def _substitute_url_secrets(
    safe_url: str,
    sentinels: Mapping[str, str],
    secrets: Mapping[str, ResolvedSecret],
) -> str:
    """Replace the sentinels with the resolved values, after the checks passed."""
    netloc = urlsplit(safe_url).netloc
    url = safe_url
    for code, sentinel in sentinels.items():
        if sentinel in netloc:
            # parse-time validation forbids this; a template that still gets
            # here must fail closed, not leak a secret into the destination
            raise ExternalCallError("a secret placeholder cannot appear in the URL host")
        url = url.replace(sentinel, quote(secrets[code].value, safe=""))
    return url


def _enforce_placements(plan: _RenderPlan, resolved: Mapping[str, ResolvedSecret]) -> None:
    """Each secret goes only into the request parts its placements allow."""
    header_secrets: frozenset[str] = (
        frozenset().union(*(refs.secrets for refs in plan.header_refs.values()))
        if plan.header_refs
        else frozenset()
    )
    demands = (
        (SecretPlacement.URL, plan.url_refs.secrets),
        (SecretPlacement.BODY, plan.body_refs.secrets),
        (SecretPlacement.HEADER, header_secrets),
    )
    for placement, codes in demands:
        for code in sorted(codes):
            allowed = resolved[code].placements
            if placement not in allowed:
                raise ExternalCallError(
                    PLACEMENT_BLOCKED_TEMPLATE.format(
                        code=code,
                        part=placement.value,
                        allowed=", ".join(sorted(member.value for member in allowed)),
                    )
                )


def _is_header_safe(value: str) -> bool:
    return all(" " <= char <= "~" for char in value)


def _scrub(body: str, secrets: Iterable[ResolvedSecret]) -> str:
    """Remove echoes of the injected secrets from text headed to the model.

    Some APIs reflect request material (echo endpoints, error pages); both
    the substituted form and the stored plain value are masked — an API that
    inverts the transform (Basic auth decodes the base64) can echo the plain
    value, not just what was sent.
    """
    for secret in secrets:
        body = body.replace(secret.value, SECRET_SCRUBBED)
        body = body.replace(secret.plain, SECRET_SCRUBBED)
    return body


def _truncate(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + TRUNCATED_SUFFIX
