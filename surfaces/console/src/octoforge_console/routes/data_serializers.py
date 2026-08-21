from typing import Any

from octoforge_core.admin.api import ExchangeOverview, SecretOverview, UserParamOverview
from octoforge_core.context.api import DialogueSummary
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.instructions.api import Instruction
from octoforge_core.memory.api import Memory

from .common import iso


def instruction(item: Instruction, names: dict[str, str] | None = None) -> dict[str, Any]:
    resolved = names or {}
    return {
        "id": item.id,
        "type": item.type.value,
        "title": item.title,
        "content": item.content,
        "tags": list(item.tags),
        "owner_id": item.owner_id,
        "owner_name": resolved.get(item.owner_id or "", ""),
        "author_id": item.author_id,
        "author_name": resolved.get(item.author_id or "", ""),
        "system": item.system,
        "version": item.version,
        "usage_count": item.usage_count,
        "success_count": item.success_count,
        "updated_at": iso(item.updated_at),
    }


def dataset(item: Dataset, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "owner_user_id": item.owner_user_id,
        "owner_name": names.get(item.owner_user_id, ""),
        "name": item.name,
        "description": item.description,
        "usage_notes": item.usage_notes,
        "retention": item.retention,
        "version": item.version,
        "fields": [field.name for field in item.schema.fields],
        "updated_at": iso(item.updated_at),
    }


def record(item: DatasetRecord, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "owner_user_id": item.owner_user_id,
        "owner_name": names.get(item.owner_user_id, ""),
        "payload": item.payload,
        "created_at": iso(item.created_at),
    }


def memory(item: Memory, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id or "", ""),
        "key": item.key,
        "content": item.content,
        "tags": list(item.tags),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def summary(item: DialogueSummary) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialog_id": item.dialog_id,
        "seq_from": item.seq_from,
        "seq_to": item.seq_to,
        "topics": list(item.topics),
        "content": item.content,
        "created_at": iso(item.created_at),
    }


def exchange(item: ExchangeOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialog_id": item.dialog_id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "channel": item.channel,
        "status": item.status.value,
        "title": item.title,
        "pending_question": item.pending_question,
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def user_param(item: UserParamOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "code": item.code,
        "value": item.value,
        "updated_at": iso(item.updated_at),
    }


def secret(item: SecretOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "code": item.code,
        "allowed_host": item.allowed_host,
        "description": item.description,
        "placements": list(item.placements),
        "transform": item.transform,
        "created_at": iso(item.created_at),
        "last_used_at": iso(item.last_used_at),
    }
