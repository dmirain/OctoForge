"""Stored instruction administration."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.admin.api import clamp_page
from octoforge_core.instructions.api import InstructionNotFoundError, SystemInstructionError
from octoforge_server import audit

from .actions import InstructionActionDep, ListingServicesDep
from .common import DELETED_STATUS, names, page_payload
from .data_serializers import instruction as serialize_instruction
from .deps import InstructionsDep, OperatorDep
from .query import PageDep

router = APIRouter()


@router.get("/instructions")
async def instructions(
    services: ListingServicesDep,
    page_query: PageDep,
    query: str | None = None,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_instructions(resolved_limit, resolved_offset, query=query)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_instruction(item, resolved_names))


@router.post("/instructions/{instruction_id}/publish")
async def publish_instruction(
    instruction_id: str, operator: OperatorDep, service: InstructionsDep
) -> dict[str, Any]:
    try:
        published = serialize_instruction(await service.publish(instruction_id))
    except InstructionNotFoundError as exc:
        audit.record(audit.AuditEvent("instruction.publish", operator, instruction_id, "not_found"))
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("instruction.publish", operator, instruction_id))
    return published


@router.delete("/instructions/{instruction_id}")
async def delete_instruction(
    instruction_id: str,
    action: InstructionActionDep,
    owner_id: str | None = None,
) -> dict[str, str]:
    try:
        if owner_id is not None:
            await action.service.delete(owner_id, instruction_id)
        else:
            await action.service.delete_public(instruction_id)
    except InstructionNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except SystemInstructionError as exc:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("instruction.delete", action.operator, instruction_id))
    return DELETED_STATUS
