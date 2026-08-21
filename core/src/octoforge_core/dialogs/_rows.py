"""Mapping and JSON-column codecs for dialog persistence."""

from typing import Any

from octoforge_core.dialogs.models import DialogRow, ExchangeRow, MessageRow
from octoforge_core.dialogs.types import Exchange, ExchangeStatus
from octoforge_core.domain import (
    Attachment,
    AttachmentKind,
    ChatMessage,
    Dialog,
    MessageKind,
    MessageRole,
    ToolCall,
)


def to_exchange(row: ExchangeRow) -> Exchange:
    return Exchange(
        row.id,
        row.dialog_id,
        ExchangeStatus(row.status),
        row.title,
        row.created_at,
        row.updated_at,
        row.pending_question,
    )


def to_dialog(row: DialogRow) -> Dialog:
    return Dialog(row.id, row.user_id, row.channel, row.created_at, row.updated_at)


def to_chat_message(row: MessageRow) -> ChatMessage:
    return ChatMessage(
        role=MessageRole(row.role),
        content=row.content,
        tool_calls=tool_calls_from_json(row.tool_calls),
        tool_call_id=row.tool_call_id,
        task_id=row.task_id,
        kind=MessageKind(row.kind) if row.kind else MessageKind.OWN,
        attachments=attachments_from_json(row.attachments),
        id=row.id,
        exchange_id=row.exchange_id,
    )


def attachments_to_json(items: tuple[Attachment, ...]) -> list[dict[str, Any]] | None:
    if not items:
        return None
    return [{"kind": item.kind.value, "ref": item.ref} for item in items]


def attachments_from_json(raw: list[dict[str, Any]] | None) -> tuple[Attachment, ...]:
    if not raw:
        return ()
    return tuple(
        Attachment(kind=AttachmentKind(item["kind"]), ref=str(item["ref"])) for item in raw
    )


def kind_to_column(kind: MessageKind) -> str | None:
    return None if kind is MessageKind.OWN else kind.value


def tool_calls_to_json(tool_calls: tuple[ToolCall, ...]) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    return [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in tool_calls]


def tool_calls_from_json(raw: list[dict[str, Any]] | None) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    return tuple(
        ToolCall(id=str(item["id"]), name=str(item["name"]), arguments=dict(item["arguments"]))
        for item in raw
    )
