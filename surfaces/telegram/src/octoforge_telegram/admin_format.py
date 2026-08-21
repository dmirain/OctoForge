"""Human-readable identity, invite and profile fragments for Telegram admin output."""

from octoforge_core.identity.api import UserIdentity

from octoforge_telegram.invites.api import Invite, MemberProfile


def who(identity: UserIdentity, profile: MemberProfile | None) -> str:
    name = identity.name or (
        " ".join(part for part in (profile.first_name, profile.last_name) if part)
        if profile is not None
        else ""
    )
    username = identity.username or (profile.username if profile is not None else None)
    handle = f"@{username}" if username else ""
    if name and handle:
        return f"{name} ({handle})"
    return name or handle


def access(handle: str, invites: list[Invite]) -> str:
    invite = next((item for item in invites if item.claimed_by == handle), None)
    if invite is None:
        return "no-invite (admin or pre-gate)"
    note = f", note: {invite.note}" if invite.note else ""
    return f"{invite.status.value} via invite {invite.code}{note}"


def stray_line(handle: str, invites: list[Invite], profile: MemberProfile | None) -> str:
    label = profile.display_name if profile is not None else handle
    return f"- {label} - {handle}: access={access(handle, invites)}"


def pending_line(invite: Invite, bot_username: str) -> str:
    note = invite.note or "no note"
    if not bot_username:
        return f"- {invite.code} ({note})"
    link = f"[@{bot_username}](https://t.me/{bot_username}?start={invite.code})"
    return f"- {link} ({note}, code {invite.code})"
