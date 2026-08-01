"""Admin console tool of the Telegram surface: users, invites, access revocation.

Registered only when the Telegram adapter is enabled and admin ids are
configured, and visible only to those admins (the `visible_to` opt-in of
`ToolRegistry.specs(context)`); the authorization check in `execute()` is the
second, defense-in-depth barrier against a direct call.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import DialogRepository, MessageRepository, MessageStats
from octoforge_core.domain import Dialog
from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    SearchHit,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_server import audit

from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX, TelegramClient
from octoforge_telegram.invites.api import (
    Invite,
    InviteStatus,
    InviteStore,
    MemberDirectory,
    MemberProfile,
)
from octoforge_telegram.poller import chat_id_from_user_id

NAME = "admin_manage"
NOT_AUTHORIZED_MESSAGE = "error: not authorized"
INVITE_NOT_FOUND_MESSAGE = "error: invite not found (give user_id or invite_id)"
USER_ID_REQUIRED_MESSAGE = "error: user_id or invite_id is required"

ACTION_LIST_USERS = "list_users"
ACTION_GENERATE_INVITE = "generate_invite"
ACTION_REVOKE_INVITE = "revoke_invite"
ACTION_RESTORE_INVITE = "restore_invite"
ACTION_SEARCH_INSTRUCTIONS = "search_instructions"
ACTION_PUBLISH_INSTRUCTION = "publish_instruction"
INVITE_LINK_TEMPLATE = "https://t.me/{bot}?start={code}"
# Without a configured handle the tool cannot build a link: only the Bot API
# knows the bot's public name, and guessing it in code would hand out a link
# into somebody else's chat.
NO_BOT_USERNAME_HINT = (
    "set OF_TELEGRAM_BOT_USERNAME to hand out a one-tap link instead of a bare code"
)
MAX_INSTRUCTION_RESULTS = 10
INSTRUCTION_SNIPPET_CHARS = 120
PUBLISH_NOT_FOUND_MESSAGE = "error: instruction not found (give the id from search_instructions)"

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
                ACTION_SEARCH_INSTRUCTIONS,
                ACTION_PUBLISH_INSTRUCTION,
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
        "query": {
            "type": "string",
            "description": "Free-text query for search_instructions (all users' records)",
        },
        "id": {
            "type": "string",
            "description": "Instruction id for publish_instruction (from search_instructions)",
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
    # the bot's public @handle (no decoration), used to build invite deep
    # links; empty means invites are handed out as bare codes
    bot_username: str = ""


@dataclass(frozen=True, slots=True)
class AdminStores:
    """Backends the admin console reads and writes through."""

    invites: InviteStore
    cron_store: CronStore
    messages: MessageRepository
    dialogs: DialogRepository
    instructions: InstructionService
    # who-is-who profiles recorded by the poller (None: not configured)
    directory: MemberDirectory | None = None


ActionHandler = Callable[[dict[str, Any]], Awaitable[str]]


def _audit_target(arguments: dict[str, Any]) -> str:
    """The id an admin action names, for the audit line (never free text)."""
    # "id" is what the tool's own schema calls the instruction/invite argument
    for key in ("id", "instruction_id", "invite_id", "user_id"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return audit.UNKNOWN_TARGET


class AdminManageTool:
    """Manages Telegram access: invite codes, user list, revoke/restore, publishing."""

    def __init__(self, stores: AdminStores, access: AdminAccess) -> None:
        self._invites = stores.invites
        self._cron = stores.cron_store
        self._messages = stores.messages
        self._dialogs = stores.dialogs
        self._instructions = stores.instructions
        self._directory = stores.directory
        self._access = access
        self._handlers: dict[str, ActionHandler] = {
            ACTION_LIST_USERS: self._list_users,
            ACTION_GENERATE_INVITE: self._generate_invite,
            ACTION_REVOKE_INVITE: self._revoke,
            ACTION_RESTORE_INVITE: self._restore,
            ACTION_SEARCH_INSTRUCTIONS: self._search_instructions,
            ACTION_PUBLISH_INSTRUCTION: self._publish_instruction,
        }

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=NAME,
            description=(
                "Administer the Telegram bot: list users (name, @username, which "
                "invite they entered through, usage stats), generate "
                "an invite, revoke a user's access (disables their cron jobs, "
                "reversible) or restore it back; search instructions across all users "
                "and publish one by id (makes it visible to everyone). Admins only. "
                "generate_invite answers with a ready-to-forward markdown link when "
                "this installation knows the bot's handle — pass that link through to "
                "the admin verbatim instead of retyping the bare code."
            ),
            parameters_schema=SCHEMA,
        )

    def visible_to(self, context: ToolContext) -> bool:
        """Expose the tool to the LLM only when the caller is a Telegram admin."""
        return self._is_admin(context)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        action = str(arguments["action"])
        if not self._is_admin(context):
            audit.record(f"admin.{action}", context.user_id, outcome="denied")
            return NOT_AUTHORIZED_MESSAGE
        handler = self._handlers.get(action)
        if handler is None:
            return f"error: unknown action {arguments['action']!r}"
        result = await handler(arguments)
        # the trail records that an operator acted and on what, never the answer
        audit.record(f"admin.{action}", context.user_id, _audit_target(arguments))
        return result

    def _is_admin(self, context: ToolContext) -> bool:
        if context.channel != TELEGRAM_CHANNEL:
            return False
        numeric_id = chat_id_from_user_id(context.user_id)
        return numeric_id is not None and numeric_id in self._access.admin_ids

    async def _list_users(self, arguments: dict[str, Any]) -> str:
        invites = await self._invites.list_all()
        stats = {
            entry.user_id: entry
            for entry in await self._messages.stats_by_channel(TELEGRAM_CHANNEL)
        }
        dialogs = await self._dialogs.list_by_channel(TELEGRAM_CHANNEL)
        profiles = await self._member_profiles()
        user_ids = sorted(
            {invite.claimed_by for invite in invites if invite.claimed_by}
            | {dialog.user_id for dialog in dialogs}
            | set(profiles)
        )
        lines = [f"telegram users: {len(user_ids)}, invites: {len(invites)}"]
        for user_id in user_ids:
            lines.append(
                await self._user_line(
                    user_id, stats.get(user_id), dialogs, invites, profiles.get(user_id)
                )
            )
        pending = [invite for invite in invites if invite.status is InviteStatus.PENDING]
        if pending:
            lines.append("pending invites:")
            lines.extend(self._pending_line(invite) for invite in pending)
        return "\n".join(lines)

    def _pending_line(self, invite: Invite) -> str:
        """One pending invite: its link when a handle is configured, else the code."""
        note = invite.note or "no note"
        link = self._invite_link(invite.code)
        if link is None:
            return f"- {invite.code} ({note})"
        return f"- {link} ({note}, code {invite.code})"

    async def _member_profiles(self) -> dict[str, MemberProfile]:
        if self._directory is None:
            return {}
        return {profile.user_id: profile for profile in await self._directory.list_all()}

    async def _user_line(
        self,
        user_id: str,
        stats: MessageStats | None,
        dialogs: list[Dialog],
        invites: list[Invite],
        profile: MemberProfile | None,
    ) -> str:
        invite = next((item for item in invites if item.claimed_by == user_id), None)
        if invite is not None:
            note = f", note: {invite.note}" if invite.note else ""
            access = f"{invite.status.value} via invite {invite.code}{note}"
        else:
            access = "no-invite (admin or pre-gate)"
        who = f" ({profile.display_name})" if profile is not None else ""
        activity = max(
            (dialog.updated_at for dialog in dialogs if dialog.user_id == user_id),
            default=None,
        )
        jobs = await self._cron.list_for_user(user_id)
        enabled_jobs = sum(1 for job in jobs if job.enabled)
        user_messages = stats.user_messages if stats is not None else 0
        user_chars = stats.user_chars if stats is not None else 0
        agent_messages = stats.agent_messages if stats is not None else 0
        agent_chars = stats.agent_chars if stats is not None else 0
        last_active = activity.isoformat() if activity is not None else "never"
        return (
            f"- {user_id}{who}: access={access}, "
            f"wrote {user_messages} messages ({user_chars} chars), "
            f"agent replied {agent_messages} ({agent_chars} chars), "
            f"last_active={last_active}, cron={enabled_jobs}/{len(jobs)} enabled"
        )

    async def _generate_invite(self, arguments: dict[str, Any]) -> str:
        invite = await self._invites.create(str(arguments.get("note") or ""))
        link = self._invite_link(invite.code)
        if link is None:
            return (
                f"invite created: {invite.code}\n"
                f"share it; the user activates it with /start {invite.code}\n"
                f"({NO_BOT_USERNAME_HINT})"
            )
        return (
            f"invite created (code {invite.code}).\n"
            f"Give the user this link exactly as written — opening it starts the bot "
            f"and claims the invite:\n{link}"
        )

    def _invite_link(self, code: str) -> str | None:
        """Markdown link claiming the invite on first /start; None without a handle.

        The label is the bot's own handle rather than a word, so relaying the
        link never injects English into an answer written in another language.
        """
        bot = self._access.bot_username
        if not bot:
            return None
        url = INVITE_LINK_TEMPLATE.format(bot=bot, code=code)
        return f"[@{bot}]({url})"

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

    async def _search_instructions(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "error: query is required"
        hits = await self._instructions.search_all(query, MAX_INSTRUCTION_RESULTS)
        if not hits:
            return "no instructions found"
        lines = [f"instructions matching {query!r} (all owners):"]
        lines.extend(_instruction_line(index, hit) for index, hit in enumerate(hits, start=1))
        return "\n".join(lines)

    async def _publish_instruction(self, arguments: dict[str, Any]) -> str:
        instruction_id = str(arguments.get("id") or "").strip()
        if not instruction_id:
            return "error: id is required"
        try:
            instruction = await self._instructions.publish(instruction_id)
        except InstructionNotFoundError:
            return PUBLISH_NOT_FOUND_MESSAGE
        return f"published: [{instruction.type.value}] {instruction.title}"

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


def _instruction_line(index: int, hit: SearchHit) -> str:
    """One search result line for the admin console: id, owner, author, type, title, snippet."""
    instruction = hit.instruction
    owner = instruction.owner_id or "public"
    author = f", author: {instruction.author_id}" if instruction.author_id else ""
    snippet = instruction.content.replace("\n", " ")[:INSTRUCTION_SNIPPET_CHARS]
    return (
        f"{index}. [{instruction.type.value}] {instruction.title}\n"
        f"   id: {instruction.id} — owner: {owner}{author}\n"
        f"   {snippet}"
    )


def _normalize_user_id(raw: str) -> str:
    return raw if raw.startswith(USER_ID_PREFIX) else f"{USER_ID_PREFIX}{raw}"
