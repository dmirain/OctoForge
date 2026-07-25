"""Operator console API: paginated reads of every entity plus a few actions.

Reads go through the core admin read model (`octoforge_core.admin`), which is
the only cross-user view in the system. Writes deliberately reuse the same
owner-scoped services the agent uses — `CronStore.set_enabled`,
`TaskStore.delete`, `InstructionService.delete`/`publish`, `MemoryStore.delete` —
so an operator action cannot bypass an invariant the agent respects.
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from octoforge_core.admin.api import (
    AdminReadModel,
    DialogOverview,
    MessageRecord,
    Page,
    clamp_page,
)
from octoforge_core.context.api import DialogueSummary
from octoforge_core.cron.api import CronJob, CronJobNotFoundError, CronStore
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionService,
    SystemInstructionError,
)
from octoforge_core.memory.api import Memory, MemoryNotFoundError, MemoryStore
from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task
from octoforge_core.tasks.store import TaskStore

from octoforge_web.deps import (
    get_admin_read_model,
    get_cron_store,
    get_instruction_service,
    get_memory_store,
    get_task_store,
    require_admin,
)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

ReadModelDep = Annotated[AdminReadModel, Depends(get_admin_read_model)]
CronStoreDep = Annotated[CronStore, Depends(get_cron_store)]
TaskStoreDep = Annotated[TaskStore, Depends(get_task_store)]
InstructionsDep = Annotated[InstructionService, Depends(get_instruction_service)]
MemoryStoreDep = Annotated[MemoryStore, Depends(get_memory_store)]
LimitDep = Annotated[int | None, Query(ge=1)]
OffsetDep = Annotated[int | None, Query(ge=0)]

DELETED_STATUS = {"status": "deleted"}

T = TypeVar("T")


def _page_payload(page: Page[T], to_dict: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    """Wire shape of a listing: items plus what the pager needs."""
    return {
        "items": [to_dict(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


@router.get("/totals")
async def totals(read_model: ReadModelDep) -> dict[str, int]:
    """Row count per entity — the console's landing page."""
    # asdict, not vars: Totals is a slots dataclass and has no __dict__
    return asdict(await read_model.totals())


@router.get("/dialogs")
async def dialogs(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_dialogs(resolved_limit, resolved_offset)
    return _page_payload(page, _dialog_to_dict)


@router.get("/dialogs/{dialog_id}/messages")
async def dialog_messages(
    dialog_id: str,
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_messages(dialog_id, resolved_limit, resolved_offset)
    return _page_payload(page, _message_to_dict)


@router.get("/tasks")
async def tasks(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
    status: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_tasks(resolved_limit, resolved_offset, status=status, kind=kind)
    return _page_payload(page, _task_to_dict)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, store: TaskStoreDep) -> dict[str, str]:
    """Drop a task row. Only for terminal rows: a live process keeps its own."""
    try:
        await store.delete(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    return DELETED_STATUS


@router.get("/cron")
async def cron_jobs(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_cron_jobs(resolved_limit, resolved_offset)
    return _page_payload(page, _cron_to_dict)


@router.post("/cron/{job_id}/enabled")
async def set_cron_enabled(
    job_id: str,
    enabled: bool,
    store: CronStoreDep,
) -> dict[str, Any]:
    """Pause or resume a job through the store's owner-checked path."""
    try:
        job = await store.get(job_id)
        await store.set_enabled(job.user_id, job_id, enabled)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    return {"id": job_id, "enabled": enabled}


@router.delete("/cron/{job_id}")
async def delete_cron_job(job_id: str, store: CronStoreDep) -> dict[str, str]:
    try:
        job = await store.get(job_id)
        await store.delete_for_user(job.user_id, job_id)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    return DELETED_STATUS


@router.get("/instructions")
async def instructions(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
    query: str | None = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_instructions(resolved_limit, resolved_offset, query=query)
    return _page_payload(page, _instruction_to_dict)


@router.post("/instructions/{instruction_id}/publish")
async def publish_instruction(instruction_id: str, service: InstructionsDep) -> dict[str, Any]:
    """Make a private record public (the same action the admin tool exposes)."""
    try:
        return _instruction_to_dict(await service.publish(instruction_id))
    except InstructionNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc


@router.delete("/instructions/{instruction_id}")
async def delete_instruction(
    instruction_id: str,
    owner_id: str,
    service: InstructionsDep,
) -> dict[str, str]:
    """Delete an owned record; system records refuse deletion, as for the agent."""
    try:
        await service.delete(owner_id, instruction_id)
    except InstructionNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except SystemInstructionError as exc:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
    return DELETED_STATUS


@router.get("/datasets")
async def datasets(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_datasets(resolved_limit, resolved_offset)
    return _page_payload(page, _dataset_to_dict)


@router.get("/datasets/{dataset_id}/records")
async def dataset_records(
    dataset_id: str,
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_dataset_records(dataset_id, resolved_limit, resolved_offset)
    return _page_payload(page, _record_to_dict)


@router.get("/memories")
async def memories(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_memories(resolved_limit, resolved_offset)
    return _page_payload(page, _memory_to_dict)


@router.delete("/memories/{key}")
async def delete_memory(
    key: str,
    store: MemoryStoreDep,
    user_id: str | None = None,
) -> dict[str, str]:
    """Delete a memory by key; `user_id` omitted means the global scope."""
    try:
        await store.delete(user_id, key)
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    return DELETED_STATUS


@router.get("/summaries")
async def summaries(
    read_model: ReadModelDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_summaries(resolved_limit, resolved_offset)
    return _page_payload(page, _summary_to_dict)


def _dialog_to_dict(item: DialogOverview) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "channel": item.channel,
        "message_count": item.message_count,
        "task_count": item.task_count,
        "last_message_at": _iso(item.last_message_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _message_to_dict(item: MessageRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "seq": item.seq,
        "role": item.role,
        "content": item.content,
        "task_id": item.task_id,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "created_at": _iso(item.created_at),
    }


def _task_to_dict(item: Task) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialog_id": item.dialog_id,
        "user_id": item.user_id,
        "channel": item.channel,
        "kind": item.kind.value,
        "title": item.title,
        "status": item.status.value,
        "input": item.input,
        "result": item.result,
        "error": item.error,
        "created_at": _iso(item.created_at),
        "finished_at": _iso(item.finished_at),
        "delivered_at": _iso(item.delivered_at),
    }


def _cron_to_dict(item: CronJob) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "channel": item.channel,
        "title": item.title,
        "schedule": item.schedule,
        "timezone": item.timezone,
        "prompt": item.prompt,
        "enabled": item.enabled,
        "one_shot": item.one_shot,
        "next_fire_at": _iso(item.next_fire_at),
        "last_fire_at": _iso(item.last_fire_at),
        "last_status": None if item.last_status is None else item.last_status.value,
        "last_error": item.last_error,
        "retry_count": item.retry_count,
    }


def _instruction_to_dict(item: Instruction) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type.value,
        "title": item.title,
        "content": item.content,
        "tags": list(item.tags),
        "owner_id": item.owner_id,
        "system": item.system,
        "version": item.version,
        "usage_count": item.usage_count,
        "success_count": item.success_count,
        "updated_at": _iso(item.updated_at),
    }


def _dataset_to_dict(item: Dataset) -> dict[str, Any]:
    return {
        "id": item.id,
        "owner_user_id": item.owner_user_id,
        "name": item.name,
        "description": item.description,
        "usage_notes": item.usage_notes,
        "retention": item.retention,
        "version": item.version,
        "fields": [field.name for field in item.schema.fields],
        "updated_at": _iso(item.updated_at),
    }


def _record_to_dict(item: DatasetRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "owner_user_id": item.owner_user_id,
        "payload": item.payload,
        "created_at": _iso(item.created_at),
    }


def _memory_to_dict(item: Memory) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "key": item.key,
        "content": item.content,
        "tags": list(item.tags),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _summary_to_dict(item: DialogueSummary) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialog_id": item.dialog_id,
        "seq_from": item.seq_from,
        "seq_to": item.seq_to,
        "topics": list(item.topics),
        "content": item.content,
        "created_at": _iso(item.created_at),
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
