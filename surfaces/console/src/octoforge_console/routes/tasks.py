"""Task listing and terminal-row deletion."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.admin.api import TaskListing, clamp_page
from octoforge_core.tasks.api import TaskNotFoundError
from octoforge_server import audit

from .actions import ListingServicesDep
from .common import DELETED_STATUS, names, page_payload
from .deps import OperatorDep, TaskStoreDep
from .query import PageDep, TaskFilterDep
from .serializers import task as serialize_task

router = APIRouter()


@router.get("/tasks")
async def tasks(
    services: ListingServicesDep,
    page_query: PageDep,
    filters: TaskFilterDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_tasks(
        TaskListing(resolved_limit, resolved_offset, filters.status, filters.kind)
    )
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_task(item, resolved_names))


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, operator: OperatorDep, store: TaskStoreDep) -> dict[str, str]:
    try:
        await store.delete(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("task.delete", operator, task_id))
    return DELETED_STATUS
