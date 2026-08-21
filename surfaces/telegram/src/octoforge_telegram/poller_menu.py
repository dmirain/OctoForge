"""Best-effort Telegram command menu and startup backlog drain."""

import httpx

from octoforge_telegram.client import TelegramApiError, TelegramClient
from octoforge_telegram.poller_text import (
    COMMAND_INVITE,
    COMMAND_SECRETS,
    INVITE_MENU_DESCRIPTION,
    SECRETS_MENU_DESCRIPTION,
)
from octoforge_telegram.poller_types import TelegramPollerOptions

LAST_UPDATE_OFFSET = -1
DRAIN_TIMEOUT_SECONDS = 0.0


async def register_commands(client: TelegramClient, options: TelegramPollerOptions) -> None:
    commands: list[tuple[str, str]] = []
    if options.secrets_link is not None:
        commands.append((COMMAND_SECRETS.removeprefix("/"), SECRETS_MENU_DESCRIPTION))
    if options.referrals is not None:
        commands.append((COMMAND_INVITE.removeprefix("/"), INVITE_MENU_DESCRIPTION))
    try:
        await client.set_my_commands(commands)
    except Exception:
        return


async def drain_backlog(client: TelegramClient) -> int | None:
    try:
        updates = await client.get_updates(LAST_UPDATE_OFFSET, DRAIN_TIMEOUT_SECONDS)
    except (httpx.HTTPError, TelegramApiError):
        return None
    return updates[-1].update_id + 1 if updates else None
