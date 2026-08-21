"""Validation and normalization of the mcp_add request."""

import re
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from octoforge_core.mcp.api import DEFAULT_AUTH_FORMAT, DEFAULT_AUTH_HEADER, McpServer
from octoforge_core.tools.errors import ToolArgumentsError

NAME_PATTERN = re.compile(r"[^a-z0-9-]+")
ALLOWED_SCHEMES = ("http", "https")


def parse_server(arguments: dict[str, Any]) -> McpServer:
    url = _normalize_url(arguments.get("url"))
    auth = _parse_auth(arguments.get("auth"))
    return McpServer(
        id=uuid.uuid4().hex,
        name=_slug(arguments.get("name"), url),
        url=url,
        auth_secret_code=auth.get("secret"),
        auth_header=auth.get("header", DEFAULT_AUTH_HEADER),
        auth_format=auth.get("format", DEFAULT_AUTH_FORMAT),
    )


def _normalize_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("url must be a non-empty string")
    parts = urlsplit(raw.strip())
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise ToolArgumentsError("url must be an absolute http(s) URL")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


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
