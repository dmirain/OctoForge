"""Map conversation and work rows to admin values."""

from octoforge_core.admin.types import ExchangeOverview, MessageRecord, TaskOverview
from octoforge_core.context.api import DialogueSummary
from octoforge_core.context.models import SummaryRow
from octoforge_core.cron.api import CronJob
from octoforge_core.cron.models import CronJobRow
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.dialogs.models import ExchangeRow, MessageRow
from octoforge_core.tasks.api import TaskKind, TaskStatus
from octoforge_core.tasks.models import TaskRow


def to_message(row: MessageRow) -> MessageRecord:
    return MessageRecord(
        row.id,
        row.dialog_id,
        row.seq,
        row.role,
        row.content,
        row.task_id,
        row.prompt_tokens,
        row.completion_tokens,
        row.created_at,
    )


def to_task(row: TaskRow, user_id: str, channel: str) -> TaskOverview:
    return TaskOverview(
        id=row.id,
        dialog_id=row.dialog_id,
        user_id=user_id,
        channel=channel,
        kind=TaskKind(row.kind),
        title=row.title,
        status=TaskStatus(row.status),
        input=row.input,
        result=row.result,
        error=row.error,
        created_at=row.created_at,
        finished_at=row.finished_at,
        delivered_at=row.delivered_at,
    )


def to_cron(row: CronJobRow) -> CronJob:
    return CronJob(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel,
        title=row.title,
        schedule=row.schedule,
        timezone=row.timezone,
        prompt=row.prompt,
        enabled=row.enabled,
        next_fire_at=row.next_fire_at,
        last_fire_at=row.last_fire_at,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        created_at=row.created_at,
        one_shot=row.one_shot,
        last_status=None if row.last_status is None else TaskStatus(row.last_status),
        last_error=row.last_error,
        retry_count=row.retry_count,
    )


def to_exchange(row: ExchangeRow, user_id: str, channel: str) -> ExchangeOverview:
    return ExchangeOverview(
        row.id,
        row.dialog_id,
        user_id,
        channel,
        ExchangeStatus(row.status),
        row.title,
        row.pending_question,
        row.created_at,
        row.updated_at,
    )


def to_summary(row: SummaryRow) -> DialogueSummary:
    return DialogueSummary(
        row.id,
        row.dialog_id,
        row.seq_from,
        row.seq_to,
        tuple(row.topics),
        row.content,
        row.created_at,
    )
