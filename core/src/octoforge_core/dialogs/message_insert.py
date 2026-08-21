"""Build message INSERT statements from typed append commands."""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, Insert, insert

from octoforge_core.dialogs._rows import attachments_to_json, kind_to_column, tool_calls_to_json
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.dialogs.requests import MessageAppend


@dataclass(frozen=True, slots=True)
class MessageRowInput:
    row_id: str
    request: MessageAppend
    seq: ColumnElement[int]


def message_insert(data: MessageRowInput) -> Insert:
    message = data.request.message
    usage = data.request.usage
    return insert(MessageRow).values(
        id=data.row_id,
        dialog_id=data.request.dialog_id,
        seq=data.seq,
        role=message.role.value,
        content=message.content,
        tool_calls=tool_calls_to_json(message.tool_calls),
        tool_call_id=message.tool_call_id,
        client_message_id=data.request.client_message_id,
        prompt_tokens=usage.prompt_tokens if usage is not None else None,
        completion_tokens=usage.completion_tokens if usage is not None else None,
        task_id=message.task_id,
        exchange_id=message.exchange_id,
        kind=kind_to_column(message.kind),
        attachments=attachments_to_json(message.attachments),
    )
