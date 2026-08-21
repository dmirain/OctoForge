"""Model-facing schema and responses of the Telegram admin tool."""

from typing import Any

NAME = "admin_manage"
NOT_AUTHORIZED_MESSAGE = "error: not authorized"
INVITE_NOT_FOUND_MESSAGE = "error: invite not found (give user_id or invite_id)"
ACTION_LIST_USERS = "list_users"
ACTION_GENERATE_INVITE = "generate_invite"
ACTION_REVOKE_INVITE = "revoke_invite"
ACTION_RESTORE_INVITE = "restore_invite"
ACTION_SEARCH_INSTRUCTIONS = "search_instructions"
ACTION_PUBLISH_INSTRUCTION = "publish_instruction"
NO_BOT_USERNAME_HINT = "set OF_TELEGRAM_BOT_USERNAME to hand out a one-tap link"
MAX_INSTRUCTION_RESULTS = 10
INSTRUCTION_SNIPPET_CHARS = 120
PUBLISH_NOT_FOUND_MESSAGE = "error: instruction not found (give the id from search)"
REVOKED_NOTICE_TEXT = "Ваш доступ к боту отозван администратором."

DESCRIPTION = (
    "Administer Telegram users and invites, revoke/restore access, search all "
    "instructions and publish one by id. Admins only."
)
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
        },
        "note": {"type": "string"},
        "user_id": {"type": "string"},
        "invite_id": {"type": "string"},
        "query": {"type": "string"},
        "id": {"type": "string"},
    },
    "required": ["action"],
}
