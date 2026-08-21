"""Tool, credential and retention capability descriptions."""

from urllib.parse import urlsplit

from octoforge_server.capability_model import Capability
from octoforge_server.config import Settings


def tool_capabilities(settings: Settings) -> tuple[Capability, ...]:
    return (
        Capability(
            "web search",
            bool(settings.serper_token),
            (
                "serper.dev"
                if settings.serper_token
                else "OF_SERPER_TOKEN is empty - the web_search tool stays hidden"
            ),
        ),
        Capability(
            "secret store",
            bool(settings.secrets_key),
            (
                f"Fernet, one-time links at {settings.resolved_public_base_url()}"
                if settings.secrets_key
                else "OF_SECRETS_KEY is empty - endpoints declaring auth.secret fail"
            ),
        ),
        Capability(
            "mcp",
            True,
            (
                "servers are user-added records (mcp_add); mirrored tools refresh "
                f"every {settings.mcp_sync_interval_seconds:.0f}s"
            ),
        ),
    )


def surface_capabilities(settings: Settings) -> tuple[Capability, ...]:
    scheme = urlsplit(settings.database_url).scheme
    dialect = scheme.split("+", 1)[0] if scheme else "unknown"
    single_writer = " (single writer: one process only)" if dialect == "sqlite" else ""
    return (
        Capability("database", True, f"{dialect}{single_writer}"),
        Capability(
            "operator credential",
            bool(settings.admin_password_hash),
            (
                f"HTTP Basic as {settings.admin_username!r}"
                if settings.admin_password_hash
                else "OF_ADMIN_PASSWORD_HASH is empty - the HTTP surface answers 503"
            ),
        ),
    )


def retention_capabilities(settings: Settings) -> tuple[Capability, ...]:
    policy = settings.retention_policy()
    return (Capability("retention", policy.enabled(), policy.describe()),)
