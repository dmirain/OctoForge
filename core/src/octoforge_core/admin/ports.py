"""Cross-user, read-only admin listing port."""

from typing import Protocol

from octoforge_core.admin.account_types import (
    SecretOverview,
    Totals,
    UsageEventOverview,
    UsageReportRow,
    UserParamOverview,
)
from octoforge_core.admin.requests import ExchangeListing, TaskListing
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


class AdminReadModel(Protocol):
    async def totals(self) -> Totals: ...

    async def list_dialogs(self, limit: int, offset: int) -> Page[DialogOverview]: ...

    async def list_messages(
        self,
        dialog_id: str,
        limit: int,
        offset: int,
    ) -> Page[MessageRecord]: ...

    async def list_tasks(self, request: TaskListing) -> Page[TaskOverview]: ...

    async def list_cron_jobs(self, limit: int, offset: int) -> Page[CronJob]: ...

    async def list_instructions(
        self,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> Page[Instruction]: ...

    async def list_datasets(self, limit: int, offset: int) -> Page[Dataset]: ...

    async def list_dataset_records(
        self,
        dataset_id: str,
        limit: int,
        offset: int,
    ) -> Page[DatasetRecord]: ...

    async def list_memories(self, limit: int, offset: int) -> Page[Memory]: ...

    async def list_summaries(self, limit: int, offset: int) -> Page[DialogueSummary]: ...

    async def list_exchanges(self, request: ExchangeListing) -> Page[ExchangeOverview]: ...

    async def list_user_params(self, limit: int, offset: int) -> Page[UserParamOverview]: ...

    async def list_secrets(self, limit: int, offset: int) -> Page[SecretOverview]: ...

    async def list_usage_events(self, limit: int, offset: int) -> Page[UsageEventOverview]: ...

    async def usage_report(self, days: int) -> list[UsageReportRow]: ...
