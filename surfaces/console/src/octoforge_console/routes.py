"""Operator console API: paginated reads of every entity plus a few actions.

Reads go through the core admin read model (`octoforge_core.admin`), which is
the only cross-user view in the system. Writes deliberately reuse the same
owner-scoped services the agent uses — `CronStore.set_enabled`,
`TaskStore.delete`, `InstructionService.delete`/`publish` — so an operator
action cannot bypass an invariant the agent respects. Memories live in the
instruction store (type=memory) and are deleted through the same facade.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from octoforge_core import ConversationManager
from octoforge_core.admin.api import (
    AdminReadModel,
    DialogOverview,
    ExchangeOverview,
    MessageRecord,
    Page,
    TaskOverview,
    clamp_page,
)
from octoforge_core.context.api import DialogueSummary, SummaryStore
from octoforge_core.cron.api import CronJob, CronJobNotFoundError, CronStore
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.dialogs.api import (
    ClaimRepository,
    DialogNotFoundError,
    DialogRepository,
    ExchangeRepository,
)
from octoforge_core.identity.api import IdentityStore
from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
    SystemInstructionError,
)
from octoforge_core.memory.api import Memory
from octoforge_core.tasks.api import TaskNotFoundError
from octoforge_core.tasks.store import TaskStore
from octoforge_server import audit
from octoforge_server.deps import (
    get_admin_read_model,
    get_claim_repository,
    get_conversation_manager,
    get_cron_store,
    get_dialog_repository,
    get_exchange_repository,
    get_identity_store,
    get_instruction_service,
    get_operator,
    get_summary_store,
    get_task_store,
    require_admin,
)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

ReadModelDep = Annotated[AdminReadModel, Depends(get_admin_read_model)]
CronStoreDep = Annotated[CronStore, Depends(get_cron_store)]
TaskStoreDep = Annotated[TaskStore, Depends(get_task_store)]
InstructionsDep = Annotated[InstructionService, Depends(get_instruction_service)]
ManagerDep = Annotated[ConversationManager, Depends(get_conversation_manager)]
DialogsDep = Annotated[DialogRepository, Depends(get_dialog_repository)]
OperatorDep = Annotated[str, Depends(get_operator)]
ExchangesDep = Annotated[ExchangeRepository, Depends(get_exchange_repository)]
IdentityStoreDep = Annotated[IdentityStore, Depends(get_identity_store)]
ClaimsDep = Annotated[ClaimRepository, Depends(get_claim_repository)]
SummariesDep = Annotated[SummaryStore, Depends(get_summary_store)]
LimitDep = Annotated[int | None, Query(ge=1)]
OffsetDep = Annotated[int | None, Query(ge=0)]

DELETED_STATUS = {"status": "deleted"}


@dataclass(frozen=True, slots=True)
class _DialogCascade:
    """Per-module deleters composed by the dialog-deletion endpoint."""

    tasks: TaskStore
    summaries: SummaryStore
    exchanges: ExchangeRepository
    claims: ClaimRepository


def _dialog_cascade(
    tasks: TaskStoreDep,
    summaries: SummariesDep,
    exchanges: ExchangesDep,
    claims: ClaimsDep,
) -> _DialogCascade:
    return _DialogCascade(tasks=tasks, summaries=summaries, exchanges=exchanges, claims=claims)


T = TypeVar("T")


def _page_payload(page: Page[T], to_dict: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    """Wire shape of a listing: items plus what the pager needs."""
    return {
        "items": [to_dict(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


async def _names(identities: IdentityStore) -> dict[str, str]:
    """user_id -> canonical name, for the person column of every listing.

    One query for the whole page rather than one per row; users whose name is
    still empty are dropped so the UI falls back to the id instead of
    rendering blank cells.
    """
    return {user.id: user.name for user in await identities.list_users() if user.name}


@router.get("/totals")
async def totals(read_model: ReadModelDep) -> dict[str, int]:
    """Row count per entity — the console's landing page."""
    # asdict, not vars: Totals is a slots dataclass and has no __dict__
    return asdict(await read_model.totals())


@router.get("/dialogs")
async def dialogs(
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_dialogs(resolved_limit, resolved_offset)
    names = await _names(identities)
    return _page_payload(page, lambda item: _dialog_to_dict(item, names))


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


@router.delete("/dialogs/{dialog_id}")
async def delete_dialog(
    dialog_id: str,
    operator: OperatorDep,
    dialogs: DialogsDep,
    manager: ManagerDep,
    stores: Annotated[_DialogCascade, Depends(_dialog_cascade)],
) -> dict[str, str]:
    """Delete a dialog with everything it owns: messages, tasks, summaries,
    exchanges.

    The live runner (if any) is stopped first, so no actor keeps writing
    into rows that are about to disappear; the next contact of the same
    (user, channel) starts a fresh dialog. Cron jobs survive: they belong
    to the user, not to the dialog, and simply wake a new one.
    """
    try:
        dialog = await dialogs.get(dialog_id)
        await manager.evict(dialog.user_id, dialog.channel)
        await stores.summaries.delete_for_dialog(dialog_id)
        await stores.tasks.delete_for_dialog(dialog_id)
        await stores.exchanges.delete_for_dialog(dialog_id)
        # last of the module deletes: the claim row references the dialog, so
        # a stale one left by a dead process would block the delete itself
        await stores.claims.delete_for_dialog(dialog_id)
        await dialogs.delete(dialog_id)
    except DialogNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record("dialog.delete", operator, dialog_id)
    return DELETED_STATUS


@router.get("/tasks")
async def tasks(  # noqa: PLR0913, PLR0917 — FastAPI dependencies plus query filters
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
    status: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_tasks(resolved_limit, resolved_offset, status=status, kind=kind)
    names = await _names(identities)
    return _page_payload(page, lambda item: _task_to_dict(item, names))


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, operator: OperatorDep, store: TaskStoreDep) -> dict[str, str]:
    """Drop a task row. Only for terminal rows: a live process keeps its own."""
    try:
        await store.delete(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record("task.delete", operator, task_id)
    return DELETED_STATUS


@router.get("/cron")
async def cron_jobs(
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_cron_jobs(resolved_limit, resolved_offset)
    names = await _names(identities)
    return _page_payload(page, lambda item: _cron_to_dict(item, names))


@router.post("/cron/{job_id}/enabled")
async def set_cron_enabled(
    job_id: str,
    enabled: bool,
    operator: OperatorDep,
    store: CronStoreDep,
) -> dict[str, Any]:
    """Pause or resume a job through the store's owner-checked path."""
    try:
        job = await store.get(job_id)
        await store.set_enabled(job.user_id, job_id, enabled)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record("cron.enabled" if enabled else "cron.paused", operator, job_id)
    return {"id": job_id, "enabled": enabled}


@router.delete("/cron/{job_id}")
async def delete_cron_job(
    job_id: str, operator: OperatorDep, store: CronStoreDep
) -> dict[str, str]:
    try:
        job = await store.get(job_id)
        await store.delete_for_user(job.user_id, job_id)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record("cron.delete", operator, job_id)
    return DELETED_STATUS


@router.get("/instructions")
async def instructions(
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
    query: str | None = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_instructions(resolved_limit, resolved_offset, query=query)
    names = await _names(identities)
    return _page_payload(page, lambda item: _instruction_to_dict(item, names))


@router.post("/instructions/{instruction_id}/publish")
async def publish_instruction(
    instruction_id: str, operator: OperatorDep, service: InstructionsDep
) -> dict[str, Any]:
    """Make a private record public (the same action the admin tool exposes)."""
    try:
        published = _instruction_to_dict(await service.publish(instruction_id))
    except InstructionNotFoundError as exc:
        audit.record("instruction.publish", operator, instruction_id, outcome="not_found")
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record("instruction.publish", operator, instruction_id)
    return published


@router.delete("/instructions/{instruction_id}")
async def delete_instruction(
    instruction_id: str,
    operator: OperatorDep,
    service: InstructionsDep,
    owner_id: str | None = None,
) -> dict[str, str]:
    """Delete a record: `owner_id` targets a private one, omitted — a public one.

    System records refuse deletion (409): the startup registry sync owns them
    and would recreate them on the next boot.
    """
    try:
        if owner_id is not None:
            await service.delete(owner_id, instruction_id)
        else:
            await service.delete_public(instruction_id)
    except InstructionNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except SystemInstructionError as exc:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
    audit.record("instruction.delete", operator, instruction_id)
    return DELETED_STATUS


@router.get("/datasets")
async def datasets(
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_datasets(resolved_limit, resolved_offset)
    names = await _names(identities)
    return _page_payload(page, lambda item: _dataset_to_dict(item, names))


@router.get("/datasets/{dataset_id}/records")
async def dataset_records(
    dataset_id: str,
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_dataset_records(dataset_id, resolved_limit, resolved_offset)
    names = await _names(identities)
    return _page_payload(page, lambda item: _record_to_dict(item, names))


@router.get("/memories")
async def memories(
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_memories(resolved_limit, resolved_offset)
    names = await _names(identities)
    return _page_payload(page, lambda item: _memory_to_dict(item, names))


@router.delete("/memories/{key}")
async def delete_memory(
    key: str,
    operator: OperatorDep,
    service: InstructionsDep,
    user_id: str | None = None,
) -> dict[str, str]:
    """Delete a memory by key; `user_id` omitted targets a legacy global entry."""
    try:
        record = await service.get_by_name(key, InstructionType.MEMORY, user_id)
        if user_id is not None and record.owner_id != user_id:
            raise InstructionNotFoundError(key)
        if record.owner_id is None:
            # legacy global memory rows are public records: the owner-scoped
            # delete cannot reach them, the registry-sync path can
            await service.delete_system(key, InstructionType.MEMORY)
        else:
            await service.delete(record.owner_id, record.id)
    except InstructionNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record("memory.delete", operator, key)
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


@router.get("/exchanges")
async def exchanges(  # noqa: PLR0913, PLR0917 — FastAPI dependencies plus query filters
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    limit: LimitDep = None,
    offset: OffsetDep = None,
    user_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(limit, offset)
    page = await read_model.list_exchanges(
        resolved_limit, resolved_offset, user_id=user_id, status=status
    )
    names = await _names(identities)
    return _page_payload(page, lambda item: _exchange_to_dict(item, names))


def _dialog_to_dict(item: DialogOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "channel": item.channel,
        "user_message_count": item.user_message_count,
        "agent_message_count": item.agent_message_count,
        "task_count": item.task_count,
        "last_message_at": _iso(item.last_message_at),
        "last_user_message_at": _iso(item.last_user_message_at),
        "user_messages_24h": item.user_messages_24h,
        "created_at": _iso(item.created_at),
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


def _task_to_dict(item: TaskOverview, names: dict[str, str]) -> dict[str, Any]:
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
        "created_at": _iso(item.created_at),
        "finished_at": _iso(item.finished_at),
        "delivered_at": _iso(item.delivered_at),
    }


def _cron_to_dict(item: CronJob, names: dict[str, str]) -> dict[str, Any]:
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
        "next_fire_at": _iso(item.next_fire_at),
        "last_fire_at": _iso(item.last_fire_at),
        "last_status": None if item.last_status is None else item.last_status.value,
        "last_error": item.last_error,
        "retry_count": item.retry_count,
    }


def _instruction_to_dict(item: Instruction, names: dict[str, str] | None = None) -> dict[str, Any]:
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
        "updated_at": _iso(item.updated_at),
    }


def _dataset_to_dict(item: Dataset, names: dict[str, str]) -> dict[str, Any]:
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
        "updated_at": _iso(item.updated_at),
    }


def _record_to_dict(item: DatasetRecord, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "owner_user_id": item.owner_user_id,
        "owner_name": names.get(item.owner_user_id, ""),
        "payload": item.payload,
        "created_at": _iso(item.created_at),
    }


def _memory_to_dict(item: Memory, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id or "", ""),
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


def _exchange_to_dict(item: ExchangeOverview, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialog_id": item.dialog_id,
        "user_id": item.user_id,
        "user_name": names.get(item.user_id, ""),
        "channel": item.channel,
        "status": item.status.value,
        "title": item.title,
        "pending_question": item.pending_question,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@router.get("/users")
async def users(identities: IdentityStoreDep) -> dict[str, Any]:
    """Who the installation knows, and which accounts each person answers on.

    A person is the unit here, not a handle: the same human may arrive from
    Telegram and from a browser, and everything they own is filed under them.
    Revoked identities are shown rather than hidden — that an account was once
    theirs is part of the answer to "who is this".
    """
    people = await identities.list_users()
    items = [
        {
            "user_id": person.id,
            "name": person.name,
            "email": person.email,
            "created_at": person.created_at.isoformat() if person.created_at else None,
            "identities": [
                {
                    "surface": item.surface,
                    "external_id": item.external_id,
                    "name": item.name,
                    "username": item.username,
                    "active": item.active,
                }
                for item in await identities.identities_of(person.id)
            ],
        }
        for person in people
    ]
    return {"items": items, "total": len(items)}
