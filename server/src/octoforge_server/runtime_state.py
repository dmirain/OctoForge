"""What a deployment hands the service once everything is assembled.

The service does not build these — it is given them, and puts them where
requests can reach them. Keeping the container here rather than in the
composition root is what lets the service be written against it without
knowing how any of it was made.

Nothing surface-specific belongs in the named fields. A Telegram store is not
something the service has an opinion about, so it travels in `surface_state`,
filled by whichever surface owns it and read by that surface's own
dependencies.
"""

from dataclasses import dataclass, field, fields
from typing import Any

from octoforge_core import ClaimRepository, ConversationManager, DialogRepository
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.context.api import SummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import ExchangeRepository
from octoforge_core.identity.api import IdentityStore
from octoforge_core.instructions.api import InstructionService
from octoforge_core.secrets.api import SecretStore
from octoforge_core.tasks.store import TaskStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_server.config import Settings
from octoforge_server.secret_links import SecretLinkService


@dataclass(slots=True)
class Runtime:
    """Assembled services shared by the HTTP app and standalone surfaces."""

    settings: Settings
    conversation_manager: ConversationManager
    channel: str
    cron_store: CronStore
    session_factory: async_sessionmaker[AsyncSession]
    task_store: TaskStore
    instructions: InstructionService
    admin_read_model: AdminReadModel
    secret_store: SecretStore | None
    secret_links: SecretLinkService
    dialogs: DialogRepository
    summary_store: SummaryStore
    exchanges: ExchangeRepository
    claims: ClaimRepository
    identity_store: IdentityStore
    #: Channels this deployment serves, gathered from installed surfaces.
    channels: frozenset[str]
    #: Extra entries an installed surface needs its own dependencies to reach.
    #: The service copies them across without knowing what they mean.
    surface_state: dict[str, Any] = field(default_factory=dict)

    def as_state(self) -> dict[str, Any]:
        """Everything to publish on the application, by the name it answers to."""
        named = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "surface_state"
        }
        return named | self.surface_state
