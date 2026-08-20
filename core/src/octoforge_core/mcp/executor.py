"""Execution of mirrored MCP tool records: the `kind:"mcp"` call delegate.

`external_call` stays the single door for stored capabilities; this delegate
is what stands behind it for MCP mirrors. Same discipline as the classic
path: light validation whose failure returns the full contract (a blind call
self-corrects in one step), per-user secret resolved with the host binding,
the value scrubbed from whatever comes back.

Validation is deliberately structural, not full JSON Schema: required keys
and the additionalProperties gate. The server validates authoritatively
anyway, and its error comes back with the contract attached just the same.
"""

from typing import Any
from urllib.parse import urlsplit

from octoforge_core.mcp.api import McpClient, McpError, McpServer, McpServerStore, parse_mirror
from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external import (
    MAX_BODY_CHARS,
    SECRET_MISSING_TEMPLATE,
    SECRETS_DISABLED_MESSAGE,
    TRUNCATED_SUFFIX,
    ExternalCallResult,
)
from octoforge_core.secrets.api import (
    ResolvedSecret,
    SecretHostMismatchError,
    SecretNotFoundError,
    SecretStore,
)

SECRET_SCRUBBED = "[secret]"
SERVER_GONE_TEMPLATE = (
    "MCP server '{server}' is not registered on this installation; its tool records are stale"
)
STALE_MIRROR_HINT = (
    "the mirrored contract may be stale — the periodic sync refreshes it, or re-run mcp_add"
)
TOOL_ERROR_TEMPLATE = "mcp tool error: {text}\nthe tool declares this contract: {contract}"


class McpMirrorCallExecutor:
    """`KindCallDelegate` for records whose content says `kind:"mcp"`."""

    def __init__(
        self,
        store: McpServerStore,
        client: McpClient,
        secrets: SecretStore | None = None,
        spill: ResponseSpill | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._secrets = secrets
        # None: oversized structured results keep the truncation below
        self._spill = spill

    async def execute(
        self, content: str, params: dict[str, Any], user_id: str | None, scope: str = ""
    ) -> ExternalCallResult:
        """Validate against the mirrored schema, call the server, cap and scrub."""
        try:
            mirror = parse_mirror(content)
        except McpError as exc:
            raise ExternalCallError(str(exc)) from exc
        server = await self._store.get_by_name(mirror.server)
        if server is None:
            raise ExternalCallError(SERVER_GONE_TEMPLATE.format(server=mirror.server))
        try:
            _validate_arguments(mirror.input_schema, params)
        except ExternalCallError as exc:
            # same self-correction contract as the classic endpoint path
            raise ExternalCallError(f"{exc}; the tool declares this contract: {content}") from exc
        secret = await self._resolve_secret(server, user_id)
        headers = (
            {server.auth_header: server.auth_format.format(value=secret.value)}
            if secret is not None
            else {}
        )
        try:
            result = await self._client.call_tool(server.url, headers, mirror.tool, params)
        except McpError as exc:
            raise ExternalCallError(
                f"MCP call failed: {exc}; {STALE_MIRROR_HINT}; "
                f"the tool declares this contract: {content}"
            ) from exc
        scrubbed = _scrub(result.text, secret)
        body: str | None = None
        if self._spill is not None and user_id is not None and not result.is_error:
            # MCP results carry no content-type; the spill sniffs JSON itself
            body = await self._spill.spill(
                owner_id=user_id,
                body=scrubbed,
                content_type="",
                source=f"mcp:{mirror.server}/{mirror.tool}",
                wire_truncated=False,
                scope=scope,
            )
        if body is None:
            body = _truncate(scrubbed)
        if result.is_error:
            body = TOOL_ERROR_TEMPLATE.format(text=body, contract=content)
        # status 0: no HTTP status worth showing — the tool renders body alone
        return ExternalCallResult(status=0, body=body)

    async def _resolve_secret(
        self, server: McpServer, user_id: str | None
    ) -> ResolvedSecret | None:
        """The caller's own token for this server, host-bound as everywhere."""
        if server.auth_secret_code is None:
            return None
        if self._secrets is None:
            raise ExternalCallError(SECRETS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError(
                "this MCP server requires a per-user secret: no user in context"
            )
        host = (urlsplit(server.url).hostname or "").lower()
        try:
            return await self._secrets.resolve(user_id, server.auth_secret_code, host)
        except SecretNotFoundError:
            raise ExternalCallError(
                SECRET_MISSING_TEMPLATE.format(code=server.auth_secret_code, host=host)
            ) from None
        except SecretHostMismatchError as exc:
            raise ExternalCallError(str(exc)) from None


def _validate_arguments(schema: dict[str, Any], params: dict[str, Any]) -> None:
    """Required keys and the additionalProperties gate; shapes are the server's."""
    required = schema.get("required")
    if isinstance(required, list):
        missing = sorted(name for name in required if isinstance(name, str) and name not in params)
        if missing:
            raise ExternalCallError(f"missing required arguments: {', '.join(missing)}")
    properties = schema.get("properties")
    if schema.get("additionalProperties") is False and isinstance(properties, dict):
        unknown = sorted(set(params) - set(properties))
        if unknown:
            raise ExternalCallError(f"unknown arguments: {', '.join(unknown)}")


def _scrub(body: str, secret: ResolvedSecret | None) -> str:
    if secret is None:
        return body
    # both forms: an API that inverts the transform (Basic auth decodes the
    # base64) can echo the plain value, not just what was sent
    return body.replace(secret.value, SECRET_SCRUBBED).replace(secret.plain, SECRET_SCRUBBED)


def _truncate(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + TRUNCATED_SUFFIX
