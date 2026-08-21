"""Borrow a subscriber's host-bound credential for background MCP sync."""

from urllib.parse import urlsplit

from octoforge_core.mcp.api import McpServer, McpServerStore
from octoforge_core.secrets.api import SecretNotFoundError, SecretStore


async def subscriber_secret(
    store: McpServerStore,
    secrets: SecretStore | None,
    server: McpServer,
) -> str | None:
    if server.auth_secret_code is None or secrets is None:
        return None
    host = (urlsplit(server.url).hostname or "").lower()
    for user_id in await store.list_user_ids(server.id):
        try:
            resolved = await secrets.resolve(user_id, server.auth_secret_code, host)
        except SecretNotFoundError:
            continue
        return resolved.value
    return None
