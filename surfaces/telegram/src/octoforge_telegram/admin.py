"""Telegram-only admin tool over users, invites and stored instructions."""

from collections.abc import Awaitable, Callable
from typing import Any

from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_server import audit

from octoforge_telegram.admin_contract import (
    ACTION_GENERATE_INVITE,
    ACTION_LIST_USERS,
    ACTION_PUBLISH_INSTRUCTION,
    ACTION_RESTORE_INVITE,
    ACTION_REVOKE_INVITE,
    ACTION_SEARCH_INSTRUCTIONS,
    DESCRIPTION,
    NAME,
    NO_BOT_USERNAME_HINT,
    NOT_AUTHORIZED_MESSAGE,
    PUBLISH_NOT_FOUND_MESSAGE,
    SCHEMA,
)
from octoforge_telegram.admin_instructions import AdminInstructionActions
from octoforge_telegram.admin_invites import AdminInviteActions
from octoforge_telegram.admin_types import AdminAccess, AdminStores
from octoforge_telegram.admin_users import AdminUserReporter
from octoforge_telegram.client import TELEGRAM_CHANNEL

ActionHandler = Callable[[dict[str, Any]], Awaitable[str]]

__all__ = [
    "ACTION_GENERATE_INVITE",
    "ACTION_LIST_USERS",
    "ACTION_PUBLISH_INSTRUCTION",
    "ACTION_RESTORE_INVITE",
    "ACTION_REVOKE_INVITE",
    "ACTION_SEARCH_INSTRUCTIONS",
    "NOT_AUTHORIZED_MESSAGE",
    "NO_BOT_USERNAME_HINT",
    "PUBLISH_NOT_FOUND_MESSAGE",
    "AdminAccess",
    "AdminManageTool",
    "AdminStores",
]


class AdminManageTool:
    def __init__(self, stores: AdminStores, access: AdminAccess) -> None:
        invites = AdminInviteActions(stores, access)
        instructions = AdminInstructionActions(stores.instructions)
        users = AdminUserReporter(stores, access)
        self._access = access
        self._handlers: dict[str, ActionHandler] = {
            ACTION_LIST_USERS: users.report,
            ACTION_GENERATE_INVITE: invites.generate,
            ACTION_REVOKE_INVITE: invites.revoke,
            ACTION_RESTORE_INVITE: invites.restore,
            ACTION_SEARCH_INSTRUCTIONS: instructions.search,
            ACTION_PUBLISH_INSTRUCTION: instructions.publish,
        }

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(NAME, DESCRIPTION, SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        return self._is_admin(context)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        action = str(arguments["action"])
        if not self._is_admin(context):
            audit.record(audit.AuditEvent(f"admin.{action}", context.user_id, outcome="denied"))
            return NOT_AUTHORIZED_MESSAGE
        handler = self._handlers.get(action)
        if handler is None:
            return f"error: unknown action {arguments['action']!r}"
        result = await handler(arguments)
        audit.record(audit.AuditEvent(f"admin.{action}", context.user_id, _audit_target(arguments)))
        return result

    def _is_admin(self, context: ToolContext) -> bool:
        return (
            context.channel == TELEGRAM_CHANNEL and context.user_id in self._access.admin_user_ids
        )


def _audit_target(arguments: dict[str, Any]) -> str:
    for key in ("id", "instruction_id", "invite_id", "user_id"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return audit.UNKNOWN_TARGET
