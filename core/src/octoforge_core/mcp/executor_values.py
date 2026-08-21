"""Argument validation and secret-safe rendering for MCP mirrors."""

from typing import Any

from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external import TRUNCATED_SUFFIX
from octoforge_core.secrets.api import ResolvedSecret

SECRET_SCRUBBED = "[secret]"


def validate_arguments(schema: dict[str, Any], params: dict[str, Any], contract: str) -> None:
    required = schema.get("required")
    missing = (
        sorted(name for name in required if isinstance(name, str) and name not in params)
        if isinstance(required, list)
        else []
    )
    properties = schema.get("properties")
    unknown = (
        sorted(set(params) - set(properties))
        if schema.get("additionalProperties") is False and isinstance(properties, dict)
        else []
    )
    if missing or unknown:
        issue = (
            f"missing required arguments: {', '.join(missing)}"
            if missing
            else f"unknown arguments: {', '.join(unknown)}"
        )
        raise ExternalCallError(f"{issue}; the tool declares this contract: {contract}")


def scrub(body: str, secret: ResolvedSecret | None) -> str:
    if secret is None:
        return body
    return body.replace(secret.value, SECRET_SCRUBBED).replace(secret.plain, SECRET_SCRUBBED)


def truncate(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + TRUNCATED_SUFFIX
