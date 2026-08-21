"""Mapping context summary and message rows to domain values."""

from octoforge_core.context.models import SummaryRow
from octoforge_core.context.types import ArchivedMessage, DialogueSummary
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.domain import MessageRole


def to_summary(row: SummaryRow) -> DialogueSummary:
    return DialogueSummary(
        id=row.id,
        dialog_id=row.dialog_id,
        seq_from=row.seq_from,
        seq_to=row.seq_to,
        topics=tuple(row.topics),
        content=row.content,
        created_at=row.created_at,
    )


def to_archived(row: MessageRow) -> ArchivedMessage:
    return ArchivedMessage(
        seq=row.seq,
        role=MessageRole(row.role),
        content=row.content,
        created_at=row.created_at,
    )
