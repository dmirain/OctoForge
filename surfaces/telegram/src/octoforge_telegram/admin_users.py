"""One coherent Telegram admin report over identities, usage and invites."""

from dataclasses import dataclass
from datetime import datetime

from octoforge_core.dialogs.api import ACTIVITY_WINDOW, MessageStats, UserActivity
from octoforge_core.identity.api import UserIdentity
from octoforge_core.time import utc_now

from octoforge_telegram.admin_format import access, pending_line, stray_line, who
from octoforge_telegram.admin_types import AdminAccess, AdminStores
from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX
from octoforge_telegram.invites.api import Invite, InviteStatus, MemberProfile


@dataclass(frozen=True, slots=True)
class IdentityLine:
    identity: UserIdentity
    handle: str
    stats: MessageStats | None
    activity: datetime | None
    writing: UserActivity | None
    invites: list[Invite]
    profile: MemberProfile | None


class AdminUserReporter:
    def __init__(self, stores: AdminStores, access_policy: AdminAccess) -> None:
        self._invites = stores.invites
        self._cron = stores.cron_store
        self._messages = stores.messages
        self._identities = stores.identities
        self._directory = stores.directory
        self._bot_username = access_policy.bot_username

    async def report(self, arguments: dict[str, object]) -> str:
        invites = await self._invites.list_all()
        identities = await self._identities.list_identities(TELEGRAM_CHANNEL)
        stats = {
            item.user_id: item for item in await self._messages.stats_by_channel(TELEGRAM_CHANNEL)
        }
        last = await self._messages.last_activity_by_channel(TELEGRAM_CHANNEL)
        writing = {
            item.user_id: item
            for item in await self._messages.user_activity_by_channel(
                TELEGRAM_CHANNEL,
                utc_now() - ACTIVITY_WINDOW,
            )
        }
        profiles = await self._profiles()
        lines = [f"telegram users: {len(identities)}, invites: {len(invites)}"]
        linked = set()
        for identity in identities:
            handle = f"{USER_ID_PREFIX}{identity.external_id}"
            linked.add(handle)
            lines.append(
                await self._identity_line(
                    IdentityLine(
                        identity,
                        handle,
                        stats.get(identity.user_id),
                        last.get(identity.user_id),
                        writing.get(identity.user_id),
                        invites,
                        profiles.get(handle),
                    )
                )
            )
        strays = sorted(
            ({invite.claimed_by for invite in invites if invite.claimed_by} | set(profiles))
            - linked
        )
        if strays:
            lines.append("accounts not yet linked to a person (first message links them):")
            lines.extend(stray_line(handle, invites, profiles.get(handle)) for handle in strays)
        pending = [invite for invite in invites if invite.status is InviteStatus.PENDING]
        if pending:
            lines.append("pending invites:")
            lines.extend(pending_line(invite, self._bot_username) for invite in pending)
        return "\n".join(lines)

    async def _profiles(self) -> dict[str, MemberProfile]:
        if self._directory is None:
            return {}
        return {profile.user_id: profile for profile in await self._directory.list_all()}

    async def _identity_line(self, data: IdentityLine) -> str:
        identity = data.identity
        jobs = await self._cron.list_for_user(identity.user_id)
        stats = data.stats
        writing = data.writing
        return (
            f"- {who(identity, data.profile) or data.handle}"
            f"{'' if identity.active else ' [identity revoked]'} - "
            f"telegram {identity.external_id}, user {identity.user_id}: "
            f"access={access(data.handle, data.invites)}, "
            f"wrote {stats.user_messages if stats else 0} messages "
            f"({stats.user_chars if stats else 0} chars, "
            f"{writing.user_messages_since if writing else 0} in last 24h), "
            f"agent replied {stats.agent_messages if stats else 0} "
            f"({stats.agent_chars if stats else 0} chars), "
            f"last_wrote={_last_wrote(writing)}, "
            f"last_active={data.activity.isoformat() if data.activity else 'never'}, "
            f"cron={sum(1 for job in jobs if job.enabled)}/{len(jobs)} enabled"
        )


def _last_wrote(writing: UserActivity | None) -> str:
    if writing is None or writing.last_user_message_at is None:
        return "never"
    return writing.last_user_message_at.isoformat()
