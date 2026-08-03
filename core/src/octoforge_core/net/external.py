"""Executor of external calls described by endpoint instruction records.

Core-side execution: the instructions module only stores/searches/ranks
endpoint records; this executor reads them through the `InstructionService`
facade, validates and renders the URL, applies the SSRF guard and the
composition-root auth whitelist, and performs the HTTP request.

An endpoint record's content may declare a `kind`, marking a contract some
other protocol executes (today: MCP tool mirrors). The executor only sniffs
the discriminator and hands the record to the delegate registered for that
kind — it knows nothing about the protocols behind them, which keeps the
dependency arrow pointing at this module.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from octoforge_core.config import DEFAULT_TIMEOUT_SECONDS
from octoforge_core.instructions.api import InstructionService, InstructionType
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.guard import SsrfGuard, matches_url_prefix
from octoforge_core.net.tool_spec import SecretAuth, ToolSpec, parse_tool_spec
from octoforge_core.secrets.api import (
    SecretHostMismatchError,
    SecretNotFoundError,
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
SECRET_MISSING_TEMPLATE = (
    "secret '{code}' is not set for this user: ask them to run /secrets in "
    "Telegram (it opens a secure form) and add the secret, then retry"
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
    """Credential sources of the executor.

    `auth_whitelist` — installation-level headers for allowlisted origins
    (from the composition root's env). `secrets` — the per-user secret store
    for endpoints declaring `auth.secret`; None disables the feature.
    """

    auth_whitelist: tuple[ExternalCallAuth, ...] = ()
    secrets: SecretStore | None = None


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
        url = _render_url(spec, validated)
        await self._guard.check(url)
        # record-declared static headers first, so the credential sources
        # below always win a name collision
        headers = dict(spec.headers)
        headers.update(self._auth_headers_for(url, user_id))
        secret_value = await self._resolve_secret(spec.secret_auth, url, user_id)
        if secret_value is not None and spec.secret_auth is not None:
            headers[spec.secret_auth.header] = spec.secret_auth.format.format(value=secret_value)
        try:
            async with self._http.stream(
                spec.method,
                url,
                headers=headers,
                content=_render_body(spec, validated),
                follow_redirects=False,  # a redirect would bypass the guard's URL check
                timeout=self._timeout,
            ) as response:
                raw, truncated = await read_capped_text(response, MAX_BODY_BYTES)
                status = response.status_code
        except httpx.HTTPError as exc:
            raise ExternalCallError(f"external call failed: {exc}") from exc
        body = _truncate(_scrub(raw, secret_value))
        if truncated and not body.endswith(TRUNCATED_SUFFIX):
            body += TRUNCATED_SUFFIX
        return ExternalCallResult(status=status, body=body)

    async def _resolve_secret(
        self,
        auth: SecretAuth | None,
        url: str,
        user_id: str | None,
    ) -> str | None:
        """Resolve the endpoint's declared secret for the target host.

        The single moment a secret value exists outside the store: it goes
        into one request header and is never logged, never returned to the
        caller and scrubbed from the response body. Failures translate into
        agent-readable guidance (the code, never the value).
        """
        if auth is None:
            return None
        if self._secrets is None:
            raise ExternalCallError(SECRETS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError("this endpoint requires a per-user secret: no user in context")
        host = (urlsplit(url).hostname or "").lower()
        try:
            return await self._secrets.resolve(user_id, auth.code, host)
        except SecretNotFoundError:
            raise ExternalCallError(SECRET_MISSING_TEMPLATE.format(code=auth.code)) from None
        except SecretHostMismatchError as exc:
            raise ExternalCallError(str(exc)) from None

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


def _render_url(spec: ToolSpec, params: dict[str, str]) -> str:
    quoted = {name: quote(value, safe="") for name, value in params.items()}
    return spec.url_template.format(**quoted)


def _render_body(spec: ToolSpec, params: dict[str, str]) -> str | None:
    """Render the request body, when the record declares one.

    Values go in verbatim — URL-escaping would corrupt an XML or JSON
    payload; format-appropriate escaping is the record author's concern.
    Secrets are never substituted here: headers only, by construction.
    """
    if spec.body_template is None:
        return None
    return spec.body_template.format(**params)


def _scrub(body: str, secret_value: str | None) -> str:
    """Remove echoes of the injected secret from the response body.

    Some APIs reflect request headers (echo endpoints, error pages); the body
    goes to the LLM and the archive, so any literal occurrence of the value
    is replaced before anyone else sees it.
    """
    if not secret_value:
        return body
    return body.replace(secret_value, SECRET_SCRUBBED)


def _truncate(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + TRUNCATED_SUFFIX
