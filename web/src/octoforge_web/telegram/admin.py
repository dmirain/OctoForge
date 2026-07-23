"""Admin console tool of the Telegram surface: users, invites, access revocation.

Registered only when the Telegram adapter is enabled and admin ids are
configured, and visible only to those admins (the `visible_to` opt-in of
`ToolRegistry.specs(context)`); the authorization check in `execute()` is the
second, defense-in-depth barrier against a direct call.
"""

from dataclasses import dataclass
from typing import Any

from octoforge_core.cron.api import CronStore
from octoforge_core.db.repositories import (
    DialogRepository,
    MessageRepository,
    MessageStats,
)
from octoforge_core.domain import Dialog
from octoforge_core.tools.base import ToolContext, ToolSpec

from octoforge_web.telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX, TelegramClient
from octoforge_web.telegram.invites.api import (
    Invite,
    InviteStatus,
    InviteStore,
)
from octoforge_web.telegram.poller import chat_id_from_user_id

NAME = "admin_manage"
NOT_AUTHORIZED_MESSAGE = "error: not authorized"
INVITE_NOT_FOUND_MESSAGE = "error: invite not found (give user_id or invite_id)"
USER_ID_REQUIRED_MESSAGE = "error: user_id or invite_id is required"

ACTION_LIST_USERS = "list_users"
ACTION_GENERATE_INVITE = "generate_invite"
ACTION_REVOKE_INVITE = "revoke_invite"
ACTION_RESTORE_INVITE = "restore_invite"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                ACTION_LIST_USERS,
                ACTION_GENERATE_INVITE,
                ACTION_REVOKE_INVITE,
                ACTION_RESTORE_INVITE,
            ],
            "description": "What to do",
        },
        "note": {
            "type": "string",
            "description": "Memo for generate_invite (who the code is for)",
        },
        "user_id": {
            "type": "string",
            "description": (
                "Target Telegram user for revoke_invite/restore_invite: 'tg:123' or plain '123'"
            ),
        },
        "invite_id": {
            "type": "string",
            "description": "Target invite id for revoke_invite/restore_invite",
        },
    },
    "required": ["action"],
}

REVOKED_NOTICE_TEXT = "Ваш доступ к боту отозван администратором."


@dataclass(frozen=True, slots=True)
class AdminAccess:
    """Who the admins are and how to reach users (optional notifications)."""

    admin_ids: frozenset[int]
    telegram: TelegramClient | None = None


class AdminManageTool:
    """Manages Telegram access: invite codes, user list, revoke/restore."""

    def __init__(
        self,
        invites: InviteStore,
        cron_store: CronStore,
        messages: MessageRepository,
        dialogs: DialogRepository,
        access: AdminAccess,
    ) -> None:
        self._invites = invites
        self._cron = cron_store
        self._messages = messages
        self._dialogs = dialogs
        self._access = access

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=NAME,
            description=(
                "Administer Telegram bot access: list users with usage stats, generate "
                "an invite code, revoke a user's access (disables their cron jobs, "
                "reversible) or restore it back. Admins only."
            ),
            parameters_schema=SCHEMA,
        )

    def visible_to(self, context: ToolContext) -> bool:
        """Expose the tool to the LLM only when the caller is a Telegram admin."""
        return self._is_admin(context)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self._is_admin(context):
            return NOT_AUTHORIZED_MESSAGE
        action = str(arguments["action"])
        if action == ACTION_LIST_USERS:
            return await self._list_users()
        if action == ACTION_GENERATE_INVITE:
            return await self._generate_invite(str(arguments.get("note") or ""))
        if action == ACTION_REVOKE_INVITE:
            return await self._revoke(arguments)
        if action == ACTION_RESTORE_INVITE:
            return await self._restore(arguments)
        return f"error: unknown action {action!r}"

    def _is_admin(self, context: ToolContext) -> bool:
        if context.channel != TELEGRAM_CHANNEL:
            return False
        numeric_id = chat_id_from_user_id(context.user_id)
        return numeric_id is not None and numeric_id in self._access.admin_ids

    async def _list_users(self) -> str:
        invites = await self._invites.list_all()
        stats = {
            entry.user_id: entry
            for entry in await self._messages.stats_by_channel(TELEGRAM_CHANNEL)
        }
        dialogs = await self._dialogs.list_by_channel(TELEGRAM_CHANNEL)
        user_ids = sorted(
            {invite.claimed_by for invite in invites if invite.claimed_by}
            | {dialog.user_id for dialog in dialogs}
        )
        lines = [f"telegram users: {len(user_ids)}, invites: {len(invites)}"]
        for user_id in user_ids:
            lines.append(await self._user_line(user_id, stats.get(user_id), dialogs, invites))
        pending = [invite for invite in invites if invite.status is InviteStatus.PENDING]
        if pending:
            lines.append("pending invite codes:")
            lines.extend(f"- {invite.code} ({invite.note or 'no note'})" for invite in pending)
        return "\n".join(lines)

    async def _user_line(
        self,
        user_id: str,
        stats: MessageStats | None,
        dialogs: list[Dialog],
        invites: list[Invite],
    ) -> str:
        invite = next((item for item in invites if item.claimed_by == user_id), None)
        access = invite.status.value if invite is not None else "no-invite"
        activity = max(
            (dialog.updated_at for dialog in dialogs if dialog.user_id == user_id),
            default=None,
        )
        jobs = await self._cron.list_for_user(user_id)
        enabled_jobs = sum(1 for job in jobs if job.enabled)
        message_count = stats.message_count if stats is not None else 0
        total_chars = stats.total_chars if stats is not None else 0
        last_active = activity.isoformat() if activity is not None else "never"
        return (
            f"- {user_id}: access={access}, messages={message_count} ({total_chars} chars), "
            f"last_active={last_active}, cron={enabled_jobs}/{len(jobs)} enabled"
        )

    async def _generate_invite(self, note: str) -> str:
        invite = await self._invites.create(note)
        return (
            f"invite created: {invite.code}\n"
            f"share it; the user activates it with /start {invite.code}"
        )

    async def _revoke(self, arguments: dict[str, Any]) -> str:
        invite = await self._find_invite(arguments)
        if invite is None:
            return INVITE_NOT_FOUND_MESSAGE
        revoked = await self._invites.revoke(invite.id)
        disabled = await self._disable_cron_jobs(revoked)
        await self._notify_revoked(revoked)
        return (
            f"revoked invite {revoked.id} (user {revoked.claimed_by}); "
            f"disabled cron jobs: {len(disabled)}"
        )

    async def _disable_cron_jobs(self, invite: Invite) -> tuple[str, ...]:
        if invite.claimed_by is None:
            return ()
        disabled: list[str] = []
        for job in await self._cron.list_for_user(invite.claimed_by):
            if job.enabled:
                await self._cron.set_enabled(invite.claimed_by, job.id, enabled=False)
                disabled.append(job.id)
        await self._invites.set_disabled_cron_jobs(invite.id, tuple(disabled))
        return tuple(disabled)

    async def _restore(self, arguments: dict[str, Any]) -> str:
        invite = await self._find_invite(arguments)
        if invite is None:
            return INVITE_NOT_FOUND_MESSAGE
        restored = await self._invites.restore(invite.id)
        reenabled = 0
        if restored.claimed_by is not None:
            for job_id in restored.disabled_cron_job_ids:
                await self._cron.set_enabled(restored.claimed_by, job_id, enabled=True)
                reenabled += 1
        await self._invites.set_disabled_cron_jobs(restored.id, ())
        return (
            f"restored invite {restored.id} (user {restored.claimed_by}); "
            f"re-enabled cron jobs: {reenabled}"
        )

    async def _find_invite(self, arguments: dict[str, Any]) -> Invite | None:
        invite_id = arguments.get("invite_id")
        if invite_id:
            return await self._invites.get_by_id(str(invite_id))
        user_id = arguments.get("user_id")
        if user_id:
            return await self._invites.get_by_user(_normalize_user_id(str(user_id)))
        return None

    async def _notify_revoked(self, invite: Invite) -> None:
        if self._access.telegram is None or invite.claimed_by is None:
            return
        chat_id = chat_id_from_user_id(invite.claimed_by)
        if chat_id is not None:
            await self._access.telegram.send_message(chat_id, REVOKED_NOTICE_TEXT)


def _normalize_user_id(raw: str) -> str:
    return raw if raw.startswith(USER_ID_PREFIX) else f"{USER_ID_PREFIX}{raw}"
