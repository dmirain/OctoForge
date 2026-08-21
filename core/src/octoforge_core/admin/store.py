"""Public SQL adapter for cross-user, read-only admin views."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin import _account_queries as accounts
from octoforge_core.admin import _dialog_queries as dialogs
from octoforge_core.admin import _knowledge_queries as knowledge
from octoforge_core.admin import _totals
from octoforge_core.admin import _usage_queries as usage
from octoforge_core.admin import _work_queries as work
from octoforge_core.admin.account_types import (
    SecretOverview,
    Totals,
    UsageEventOverview,
    UsageReportRow,
    UserParamOverview,
)
from octoforge_core.admin.requests import ExchangeListing, PageRequest, TaskListing
from octoforge_core.admin.types import (
    DialogOverview,
    ExchangeOverview,
    MessageRecord,
    Page,
    TaskOverview,
)
from octoforge_core.context.api import DialogueSummary
from octoforge_core.cron.api import CronJob
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.instructions.api import Instruction
from octoforge_core.memory.api import Memory


class SqlAlchemyAdminStore:
    """Read every admin projection through cohesive query modules."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def totals(self) -> Totals:
        return await _totals.totals(self._sessions)

    async def list_dialogs(self, limit: int, offset: int) -> Page[DialogOverview]:
        return await dialogs.list_dialogs(self._sessions, PageRequest(limit, offset))

    async def list_messages(self, dialog_id: str, limit: int, offset: int) -> Page[MessageRecord]:
        return await dialogs.list_messages(self._sessions, dialog_id, PageRequest(limit, offset))

    async def list_tasks(self, request: TaskListing) -> Page[TaskOverview]:
        return await work.list_tasks(self._sessions, request)

    async def list_cron_jobs(self, limit: int, offset: int) -> Page[CronJob]:
        return await work.list_cron_jobs(self._sessions, PageRequest(limit, offset))

    async def list_instructions(
        self,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> Page[Instruction]:
        return await knowledge.list_instructions(self._sessions, PageRequest(limit, offset), query)

    async def list_datasets(self, limit: int, offset: int) -> Page[Dataset]:
        return await knowledge.list_datasets(self._sessions, PageRequest(limit, offset))

    async def list_dataset_records(
        self,
        dataset_id: str,
        limit: int,
        offset: int,
    ) -> Page[DatasetRecord]:
        return await knowledge.list_records(
            self._sessions,
            dataset_id,
            PageRequest(limit, offset),
        )

    async def list_memories(self, limit: int, offset: int) -> Page[Memory]:
        return await knowledge.list_memories(self._sessions, PageRequest(limit, offset))

    async def list_summaries(self, limit: int, offset: int) -> Page[DialogueSummary]:
        return await work.list_summaries(self._sessions, PageRequest(limit, offset))

    async def list_exchanges(self, request: ExchangeListing) -> Page[ExchangeOverview]:
        return await work.list_exchanges(self._sessions, request)

    async def list_user_params(self, limit: int, offset: int) -> Page[UserParamOverview]:
        return await accounts.list_user_params(self._sessions, PageRequest(limit, offset))

    async def list_secrets(self, limit: int, offset: int) -> Page[SecretOverview]:
        return await accounts.list_secrets(self._sessions, PageRequest(limit, offset))

    async def list_usage_events(self, limit: int, offset: int) -> Page[UsageEventOverview]:
        return await usage.list_usage_events(self._sessions, PageRequest(limit, offset))

    async def usage_report(self, days: int) -> list[UsageReportRow]:
        return await usage.usage_report(self._sessions, days)
