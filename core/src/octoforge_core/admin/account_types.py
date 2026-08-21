"""Account metadata, usage and totals returned by the admin read model."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserParamOverview:
    user_id: str
    code: str
    value: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UsageEventOverview:
    user_id: str
    kind: str
    origin: str
    prompt_tokens: int
    completion_tokens: int
    quantity: int
    dialog_id: str | None
    exchange_id: str | None
    task_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UsageReportRow:
    user_id: str
    kind: str
    origin: str
    prompt_tokens: int
    completion_tokens: int
    quantity: int
    events: int


@dataclass(frozen=True, slots=True)
class SecretOverview:
    user_id: str
    code: str
    allowed_host: str
    description: str
    placements: tuple[str, ...]
    transform: str | None
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class Totals:
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
