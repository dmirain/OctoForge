"""Boundary of the admin read model: cross-user, read-only listings.

Every other module scopes its queries to one owner — that is the isolation rule
of the agent surfaces. An operator console needs the opposite: every dialog,
every task, every record, paginated. Rather than let the web adapter reach for
SQLAlchemy (it never does elsewhere), that view lives here behind a Protocol,
with the SQL implementation next to the other stores.

Read-only by design. Mutations stay on the existing owner-scoped services
(`CronStore.set_enabled`, `InstructionService.delete`, `TaskStore.delete`, …),
so an admin action goes through the same invariants a user action does.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from octoforge_core.context.api import DialogueSummary
from octoforge_core.cron.api import CronJob
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.instructions.api import Instruction
from octoforge_core.memory.api import Memory
from octoforge_core.tasks.api import Task

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One slice of a listing plus the total behind it (for paging in the UI).

    Spelled with `Generic[T]` rather than PEP 695 `class Page[T]`: the projects
    target Python 3.11 and CI runs it.
    """

    items: tuple[T, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class DialogOverview:
    """A dialog with the counters an operator wants before opening it."""

    id: str
    user_id: str
    channel: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    task_count: int
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """A persisted narrative message, with the row metadata the DTO hides.

    `ChatMessage` deliberately drops the row id and timestamps (it is the LLM
    wire shape); the console needs them to show and order a dialog.
    """

    id: str
    dialog_id: str
    seq: int
    role: str
    content: str
    task_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExchangeOverview:
    """An exchange with the dialog identity an operator needs to place it.

    `Exchange` (the `dialogs` module's own DTO) doesn't carry user_id/channel
    — only tasks denormalize those onto their own row. This DTO joins in what
    `DialogRow` knows, the same way `DialogOverview` does for the dialogs
    listing.
    """

    id: str
    dialog_id: str
    user_id: str
    channel: str
    status: ExchangeStatus
    title: str
    owner_task_id: str | None
    pending_question: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Totals:
    """Row counts per entity: the console's landing page."""

    dialogs: int
    messages: int
    tasks: int
    cron_jobs: int
    instructions: int
    datasets: int
    dataset_records: int
    memories: int
    dialog_summaries: int
    exchanges: int


class AdminReadModel(Protocol):
    """Port of the admin console: paginated cross-user reads, no writes."""

    async def totals(self) -> Totals:
        """Return the row count of every entity."""
        ...

    async def list_dialogs(self, limit: int, offset: int) -> Page[DialogOverview]:
        """Dialogs newest-activity first, with message and task counters."""
        ...

    async def list_messages(self, dialog_id: str, limit: int, offset: int) -> Page[MessageRecord]:
        """Messages of one dialog ordered by seq."""
        ...

    async def list_tasks(
        self,
        limit: int,
        offset: int,
        status: str | None = None,
        kind: str | None = None,
    ) -> Page[Task]:
        """Tasks newest first, optionally filtered by status and kind."""
        ...

    async def list_cron_jobs(self, limit: int, offset: int) -> Page[CronJob]:
        """Cron jobs of every user, next firing first."""
        ...

    async def list_instructions(
        self,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> Page[Instruction]:
        """Instruction records of every owner; `query` matches the title."""
        ...

    async def list_datasets(self, limit: int, offset: int) -> Page[Dataset]:
        """Dataset descriptors of every owner."""
        ...

    async def list_dataset_records(
        self,
        dataset_id: str,
        limit: int,
        offset: int,
    ) -> Page[DatasetRecord]:
        """Records of one dataset, newest first."""
        ...

    async def list_memories(self, limit: int, offset: int) -> Page[Memory]:
        """Memories of every owner, global ones included."""
        ...

    async def list_summaries(self, limit: int, offset: int) -> Page[DialogueSummary]:
        """Dialog summaries (compaction output) of every dialog."""
        ...

    async def list_exchanges(
        self,
        limit: int,
        offset: int,
        user_id: str | None = None,
        status: str | None = None,
    ) -> Page[ExchangeOverview]:
        """Exchanges of every dialog, most recently updated first.

        `user_id` and `status` narrow the listing; both are optional.
        """
        ...


def clamp_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    """Normalize paging parameters coming from a wire boundary."""
    resolved_limit = DEFAULT_PAGE_SIZE if limit is None else max(1, min(limit, MAX_PAGE_SIZE))
    resolved_offset = max(0, offset or 0)
    return resolved_limit, resolved_offset
