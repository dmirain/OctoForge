"""Dependencies and access policy of the Telegram admin tool."""

from dataclasses import dataclass

from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import MessageRepository
from octoforge_core.identity.api import IdentityStore
from octoforge_core.instructions.api import InstructionService

from octoforge_telegram.client import TelegramClient
from octoforge_telegram.invites.api import InviteStore, MemberDirectory


@dataclass(frozen=True, slots=True)
class AdminAccess:
    admin_user_ids: frozenset[str]
    telegram: TelegramClient | None = None
    bot_username: str = ""


@dataclass(frozen=True, slots=True)
class AdminStores:
    invites: InviteStore
    cron_store: CronStore
    messages: MessageRepository
    instructions: InstructionService
    identities: IdentityStore
    directory: MemberDirectory | None = None
