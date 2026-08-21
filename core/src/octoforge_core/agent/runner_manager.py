"""Public manager facade over dialog construction, ownership and recovery."""

from octoforge_core.cron.api import CronWake, WakeOutcome
from octoforge_core.dialogs.api import MessageRepository

from .runner_api import DialogSurface, RunnerConfig
from .runner_facade import ConversationRunner
from .runner_manager_background import ManagerBackground
from .runner_manager_lifecycle import ManagerLifecycle
from .runner_manager_ownership import OwnershipHeartbeat
from .runner_manager_pool import RunnerPool
from .runner_manager_recovery import ManagerRecovery
from .runner_manager_state import ManagerState, ManagerStores, OwnershipConfig
from .runner_recovery_queries import RecoveryQueries


class ConversationManager:
    """Owns one claimed actor per dialog and recovers work during handover."""

    def __init__(
        self,
        config: RunnerConfig,
        stores: ManagerStores,
        ownership: OwnershipConfig,
    ) -> None:
        self._state = ManagerState(config, stores, ownership)
        self._pool = RunnerPool(self)
        self._queries = RecoveryQueries(self)
        self._recovery = ManagerRecovery(self)
        self._background = ManagerBackground(self)
        self._ownership = OwnershipHeartbeat(self)
        self._lifecycle = ManagerLifecycle(self)

    async def get_or_create_runner(self, user_id: str, channel: str) -> ConversationRunner:
        return await self._pool.get_or_create(user_id, channel)

    async def promote_collection(self, user_id: str, channel: str, exchange_id: str) -> None:
        await self._background.promote(user_id, channel, exchange_id)

    async def wake(self, request: CronWake) -> WakeOutcome:
        return await self._background.wake(request)

    def use_surface(self, surface: DialogSurface) -> None:
        self._state.surface = surface

    def start(self) -> None:
        self._ownership.start()

    async def recover_interrupted(self) -> None:
        await self._recovery.recover_interrupted()

    async def evict(self, user_id: str, channel: str) -> None:
        await self._lifecycle.evict(user_id, channel)

    async def stop_all(self) -> None:
        await self._lifecycle.stop_all()

    async def _beat_once(self) -> None:
        await self._ownership.beat_once()

    @property
    def _messages(self) -> MessageRepository:
        return self._state.stores.messages

    @_messages.setter
    def _messages(self, messages: MessageRepository) -> None:
        self._state.stores.messages = messages

    @property
    def _surface(self) -> DialogSurface | None:
        return self._state.surface
