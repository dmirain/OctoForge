"""Generate, revoke and restore Telegram invites and their cron effects."""

from typing import Any

from octoforge_core.cron.api import CronEnablement

from octoforge_telegram.admin_contract import (
    INVITE_NOT_FOUND_MESSAGE,
    NO_BOT_USERNAME_HINT,
    REVOKED_NOTICE_TEXT,
)
from octoforge_telegram.admin_types import AdminAccess, AdminStores
from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX, SendMessage
from octoforge_telegram.invites.api import Invite
from octoforge_telegram.membership import chat_id_from_user_id


class AdminInviteActions:
    def __init__(self, stores: AdminStores, access: AdminAccess) -> None:
        self._invites = stores.invites
        self._cron = stores.cron_store
        self._identities = stores.identities
        self._access = access

    async def generate(self, arguments: dict[str, Any]) -> str:
        invite = await self._invites.create(str(arguments.get("note") or ""))
        link = self._invite_link(invite.code)
        if link is None:
            return (
                f"invite created: {invite.code}\n"
                f"share it; the user activates it with /start {invite.code}\n"
                f"({NO_BOT_USERNAME_HINT})"
            )
        return f"invite created (code {invite.code}).\nGive the user this link:\n{link}"

    async def revoke(self, arguments: dict[str, Any]) -> str:
        invite = await self._find(arguments)
        if invite is None:
            return INVITE_NOT_FOUND_MESSAGE
        revoked = await self._invites.revoke(invite.id)
        disabled = await self._disable_cron_jobs(revoked)
        await self._notify_revoked(revoked)
        return (
            f"revoked invite {revoked.id} (user {revoked.claimed_by}); "
            f"disabled cron jobs: {len(disabled)}"
        )

    async def restore(self, arguments: dict[str, Any]) -> str:
        invite = await self._find(arguments)
        if invite is None:
            return INVITE_NOT_FOUND_MESSAGE
        restored = await self._invites.restore(invite.id)
        person = await self._person_of(restored)
        reenabled = 0
        if person is not None:
            for job_id in restored.disabled_cron_job_ids:
                await self._cron.set_enabled(CronEnablement(person, job_id, True))
                reenabled += 1
        await self._invites.set_disabled_cron_jobs(restored.id, ())
        return (
            f"restored invite {restored.id} (user {restored.claimed_by}); "
            f"re-enabled cron jobs: {reenabled}"
        )

    async def _disable_cron_jobs(self, invite: Invite) -> tuple[str, ...]:
        person = await self._person_of(invite)
        if person is None:
            return ()
        disabled = []
        for job in await self._cron.list_for_user(person):
            if job.enabled:
                await self._cron.set_enabled(CronEnablement(person, job.id, False))
                disabled.append(job.id)
        await self._invites.set_disabled_cron_jobs(invite.id, tuple(disabled))
        return tuple(disabled)

    async def _person_of(self, invite: Invite) -> str | None:
        if invite.claimed_by is None:
            return None
        external_id = invite.claimed_by.removeprefix(USER_ID_PREFIX)
        identity = await self._identities.find_by_identity(TELEGRAM_CHANNEL, external_id)
        return None if identity is None else identity.user_id

    async def _find(self, arguments: dict[str, Any]) -> Invite | None:
        if arguments.get("invite_id"):
            return await self._invites.get_by_id(str(arguments["invite_id"]))
        if arguments.get("user_id"):
            user_id = str(arguments["user_id"])
            handle = user_id if user_id.startswith(USER_ID_PREFIX) else f"{USER_ID_PREFIX}{user_id}"
            return await self._invites.get_by_user(handle)
        return None

    async def _notify_revoked(self, invite: Invite) -> None:
        if self._access.telegram is None or invite.claimed_by is None:
            return
        chat_id = chat_id_from_user_id(invite.claimed_by)
        if chat_id is not None:
            await self._access.telegram.send_message(SendMessage(chat_id, REVOKED_NOTICE_TEXT))

    def _invite_link(self, code: str) -> str | None:
        bot = self._access.bot_username
        return f"[@{bot}](https://t.me/{bot}?start={code})" if bot else None
