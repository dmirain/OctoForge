"""Operator authentication and audit identity dependencies."""

from fastapi import Request

from octoforge_server.state_deps import get_auth_gate, get_settings


async def require_admin(request: Request) -> None:
    await get_auth_gate(request).authenticate(request)


def get_operator(request: Request) -> str:
    username = get_settings(request).admin_username
    client = request.client.host if request.client is not None else "unknown"
    return f"{username}@{client}"
