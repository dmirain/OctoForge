"""Request-scoped access to this surface's own stores.

They live on the application because the deployment put them there
(`Runtime.surface_state`), and they are read here rather than in the
service's dependencies: what a Telegram member directory is, and whether one
exists at all, is this surface's business and nobody else's.
"""

from typing import cast

from fastapi import Request

from octoforge_web.telegram.invites.api import InviteStore, MemberDirectory


def get_members(request: Request) -> "MemberDirectory | None":
    """The member directory, or None when the bot is not configured."""
    return cast("MemberDirectory | None", request.app.state.telegram_members)


def get_invites(request: Request) -> "InviteStore | None":
    """The invite store, or None when the bot is not configured."""
    return cast("InviteStore | None", request.app.state.telegram_invites)
