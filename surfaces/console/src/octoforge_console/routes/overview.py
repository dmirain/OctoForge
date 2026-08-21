"""Installation totals and dialog administration."""

from dataclasses import asdict
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.admin.api import clamp_page
from octoforge_core.dialogs.api import DialogNotFoundError
from octoforge_server import audit

from .actions import DialogActionDep, ListingServicesDep
from .common import DELETED_STATUS, names, page_payload
from .deps import ReadModelDep
from .query import PageDep
from .serializers import dialog as serialize_dialog
from .serializers import message as serialize_message

router = APIRouter()


@router.get("/totals")
async def totals(read_model: ReadModelDep) -> dict[str, int]:
    return asdict(await read_model.totals())


@router.get("/dialogs")
async def dialogs(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_dialogs(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_dialog(item, resolved_names))


@router.get("/dialogs/{dialog_id}/messages")
async def dialog_messages(
    dialog_id: str,
    read_model: ReadModelDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await read_model.list_messages(dialog_id, resolved_limit, resolved_offset)
    return page_payload(page, serialize_message)


@router.delete("/dialogs/{dialog_id}")
async def delete_dialog(
    dialog_id: str,
    action: DialogActionDep,
) -> dict[str, str]:
    runtime = action.runtime
    try:
        dialog = await runtime.dialogs.get(dialog_id)
        await runtime.manager.evict(dialog.user_id, dialog.channel)
        await runtime.stores.summaries.delete_for_dialog(dialog_id)
        await runtime.stores.tasks.delete_for_dialog(dialog_id)
        await runtime.stores.exchanges.delete_for_dialog(dialog_id)
        await runtime.stores.claims.delete_for_dialog(dialog_id)
        await runtime.dialogs.delete(dialog_id)
    except DialogNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("dialog.delete", action.operator, dialog_id))
    return DELETED_STATUS
