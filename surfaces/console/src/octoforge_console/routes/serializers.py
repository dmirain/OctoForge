"""Wire serializers for dialogs, messages, tasks and cron jobs."""

from typing import Any

from octoforge_core.admin.api import DialogOverview, MessageRecord, TaskOverview
from octoforge_core.cron.api import CronJob

from .common import iso


def dialog(item: DialogOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "channel": item.channel,
        "user_message_count": item.user_message_count,
        "agent_message_count": item.agent_message_count,
        "task_count": item.task_count,
        "last_message_at": iso(item.last_message_at),
        "last_user_message_at": iso(item.last_user_message_at),
        "user_messages_24h": item.user_messages_24h,
        "created_at": iso(item.created_at),
    }


def message(item: MessageRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "seq": item.seq,
        "role": item.role,
        "content": item.content,
        "task_id": item.task_id,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "created_at": iso(item.created_at),
    }


def task(item: TaskOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialog_id": item.dialog_id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "channel": item.channel,
        "kind": item.kind.value,
        "title": item.title,
        "status": item.status.value,
        "input": item.input,
        "result": item.result,
        "error": item.error,
        "created_at": iso(item.created_at),
        "finished_at": iso(item.finished_at),
        "delivered_at": iso(item.delivered_at),
    }


def cron(item: CronJob, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "channel": item.channel,
        "title": item.title,
        "schedule": item.schedule,
        "timezone": item.timezone,
        "prompt": item.prompt,
        "enabled": item.enabled,
        "one_shot": item.one_shot,
        "next_fire_at": iso(item.next_fire_at),
        "last_fire_at": iso(item.last_fire_at),
        "last_status": None if item.last_status is None else item.last_status.value,
        "last_error": item.last_error,
        "retry_count": item.retry_count,
    }
