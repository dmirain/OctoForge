"""The one agent-facing MCP tool: mcp_add.

Everything else reuses machinery that already exists — discovery is
`recall(type=endpoint)`, the contract is `endpoint_get`, execution is
`external_call`. Adding a server is the only genuinely new verb.
"""

import re
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from octoforge_core.mcp.api import (
    DEFAULT_AUTH_FORMAT,
    DEFAULT_AUTH_HEADER,
    McpError,
    McpServer,
    McpServerNameTakenError,
    McpServerStore,
)
from octoforge_core.mcp.skills import SkillPattern
from octoforge_core.mcp.sync import McpToolSync, mirror_title
from octoforge_core.net.errors import SsrfBlockedError
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.secrets.api import SecretNotFoundError, SecretStore
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

NAME = "mcp_add"
DESCRIPTION = (
    "Register an external MCP server (Streamable HTTP URL) for this installation "
    "and mirror its tools into endpoint records. The same URL added twice — by "
    "anyone — stays one server; its tools are shared. After adding, discover the "
    "tools with recall(type=endpoint), read a contract with endpoint_get and "
    "execute with external_call. If the server needs a token, declare auth with a "
    "secret code: every user stores their OWN token under that code via /secrets — "
    "there are no shared credentials. A periodic sync keeps the mirrored tools "
    "fresh; re-run mcp_add to force a refresh."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The server's Streamable HTTP endpoint, e.g. https://mcp.example.com/mcp",
        },
        "name": {
            "type": "string",
            "description": "Short slug for the server (defaults to a slug of its hostname)",
        },
        "auth": {
            "type": "object",
            "description": "Only for servers that require a token",
            "properties": {
                "secret": {
                    "type": "string",
                    "description": "Per-user secret code the token is stored under",
                },
                "header": {"type": "string", "description": "Header name (default Authorization)"},
                "format": {
                    "type": "string",
                    "description": "Header value template with {value} (default 'Bearer {value}')",
                },
            },
            "required": ["secret"],
        },
    },
    "required": ["url"],
}

MAX_LISTED_TOOLS = 20
NAME_PATTERN = re.compile(r"[^a-z0-9-]+")
ALLOWED_SCHEMES = ("http", "https")
SECRET_HINT_TEMPLATE = (
    "the server declares auth via secret '{code}': each user adds their own "
    "token with /secrets before calling"
)
SYNC_FAILED_TEMPLATE = (
    "MCP server '{name}' registered, but listing its tools failed: {error}. "
    "The periodic sync will retry; fix the cause (URL, token) and re-run "
    "mcp_add to force it."
)


class McpAddTool:
    """Registers an MCP server and runs its first mirror sync."""

    def __init__(
        self,
        store: McpServerStore,
        sync: McpToolSync,
        guard: SsrfGuard,
        secrets: SecretStore | None = None,
    ) -> None:
        self._store = store
        self._sync = sync
        self._guard = guard
        self._secrets = secrets

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=NAME, description=DESCRIPTION, parameters_schema=SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        """Hide the tool when the caller's plan does not include MCP servers."""
        return feature_enabled(context.enabled_features, FeatureCode.MCP_ADD)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Normalize, guard, register (dedup by URL), then mirror the tools."""
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.MCP_ADD)
        url = _normalize_url(arguments.get("url"))
        try:
            await self._guard.check(url)
        except SsrfBlockedError as exc:
            return f"error: {exc}"
        auth = _parse_auth(arguments.get("auth"))
        server = McpServer(
            id=uuid.uuid4().hex,
            name=_slug(arguments.get("name"), url),
            url=url,
            auth_secret_code=auth.get("secret"),
            auth_header=auth.get("header", DEFAULT_AUTH_HEADER),
            auth_format=auth.get("format", DEFAULT_AUTH_FORMAT),
        )
        try:
            added = await self._store.add(server, context.user_id)
        except McpServerNameTakenError as exc:
            return f"error: server name {exc} already points at a different URL; pass another name"
        secret_value = await self._callers_secret(added, context.user_id)
        try:
            outcome = await self._sync.sync_server(added, secret_value)
        except McpError as exc:
            report = SYNC_FAILED_TEMPLATE.format(name=added.name, error=exc)
            return self._with_secret_hint(added, report)
        listed = ", ".join(
            mirror_title(added.name, tool_name)
            for tool_name in outcome.tool_names[:MAX_LISTED_TOOLS]
        )
        skill_note = ""
        if outcome.skill is not None and outcome.skill is not SkillPattern.UNRECOGNIZED:
            skill_note = (
                f" A usage skill 'mcp/{added.name}' was written "
                f"({outcome.skill.value} shape) — recall finds it without a type filter."
            )
        report = (
            f"MCP server '{added.name}' ready: {outcome.tools} tools mirrored as endpoint "
            f"records (find them with recall(type=endpoint), execute with external_call)."
            + (f" Tools: {listed}" if listed else "")
            + skill_note
        )
        return self._with_secret_hint(added, report)

    def _with_secret_hint(self, server: McpServer, report: str) -> str:
        if server.auth_secret_code is None:
            return report
        return f"{report}\n{SECRET_HINT_TEMPLATE.format(code=server.auth_secret_code)}"

    async def _callers_secret(self, server: McpServer, user_id: str) -> str | None:
        """The adding user's own token, when they have one; never an error.

        A missing token must not block registration — the sync simply runs
        unauthenticated and reports what the server said.
        """
        if server.auth_secret_code is None or self._secrets is None:
            return None
        host = (urlsplit(server.url).hostname or "").lower()
        try:
            resolved = await self._secrets.resolve(user_id, server.auth_secret_code, host)
        except SecretNotFoundError:
            return None
        return resolved.value


def _normalize_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("url must be a non-empty string")
    parts = urlsplit(raw.strip())
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise ToolArgumentsError("url must be an absolute http(s) URL")
    # lowercase the scheme/host and drop a trailing slash: the URL is the
    # server's identity, and cosmetic variants must dedup onto one row
    netloc = (parts.netloc or "").lower()
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), parts.query, ""))


def _slug(raw: object, url: str) -> str:
    if raw is not None and (not isinstance(raw, str) or not raw.strip()):
        raise ToolArgumentsError("name must be a non-empty string")
    source = raw.strip() if isinstance(raw, str) else (urlsplit(url).hostname or "server")
    slug = NAME_PATTERN.sub("-", source.lower()).strip("-")
    if not slug:
        raise ToolArgumentsError("name must contain letters or digits")
    return slug


def _parse_auth(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolArgumentsError("auth must be an object")
    secret = raw.get("secret")
    if not isinstance(secret, str) or not secret.strip():
        raise ToolArgumentsError("auth.secret must be a non-empty secret code")
    auth = {"secret": secret.strip()}
    header = raw.get("header")
    if header is not None:
        if not isinstance(header, str) or not header.strip():
            raise ToolArgumentsError("auth.header must be a non-empty string")
        auth["header"] = header.strip()
    value_format = raw.get("format")
    if value_format is not None:
        if not isinstance(value_format, str) or "{value}" not in value_format:
            raise ToolArgumentsError("auth.format must contain the {value} placeholder")
        auth["format"] = value_format
    return auth
