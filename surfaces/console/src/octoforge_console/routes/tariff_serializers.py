"""Wire serializers for plans and usage events."""

from typing import Any

from octoforge_core.admin.api import UsageEventOverview
from octoforge_core.tariffs.api import Tariff

from .common import iso


def tariff(item: Tariff) -> dict[str, Any]:
    return {
        "code": item.code,
        "title": item.title,
        "features": sorted(item.features),
        "daily_tokens": item.limits.daily_tokens,
        "daily_user_messages": item.limits.daily_user_messages,
        "daily_assistant_messages": item.limits.daily_assistant_messages,
        "max_cron_jobs": item.limits.max_cron_jobs,
        "max_datasets": item.limits.max_datasets,
        "max_memory_chars": item.limits.max_memory_chars,
        "is_default": item.is_default,
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def usage_event(item: UsageEventOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "kind": item.kind,
        "origin": item.origin,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "quantity": item.quantity,
        "dialog_id": item.dialog_id,
        "exchange_id": item.exchange_id,
        "task_id": item.task_id,
        "created_at": iso(item.created_at),
    }
