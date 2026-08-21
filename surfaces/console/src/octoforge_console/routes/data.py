"""Dataset, memory, summary and exchange administration."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.admin.api import ExchangeListing, clamp_page
from octoforge_core.instructions.api import InstructionNotFoundError, InstructionType
from octoforge_server import audit

from .actions import InstructionActionDep, ListingServicesDep
from .common import DELETED_STATUS, names, page_payload
from .data_serializers import dataset as serialize_dataset
from .data_serializers import exchange as serialize_exchange
from .data_serializers import memory as serialize_memory
from .data_serializers import record as serialize_record
from .data_serializers import summary as serialize_summary
from .deps import ReadModelDep
from .query import ExchangeFilterDep, PageDep

router = APIRouter()


@router.get("/datasets")
async def datasets(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_datasets(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_dataset(item, resolved_names))


@router.get("/datasets/{dataset_id}/records")
async def dataset_records(
    dataset_id: str,
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_dataset_records(
        dataset_id, resolved_limit, resolved_offset
    )
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_record(item, resolved_names))


@router.get("/memories")
async def memories(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_memories(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_memory(item, resolved_names))


@router.delete("/memories/{key}")
async def delete_memory(
    key: str, action: InstructionActionDep, user_id: str | None = None
) -> dict[str, str]:
    try:
        record = await action.service.get_by_name(key, InstructionType.MEMORY, user_id)
        if user_id is not None and record.owner_id != user_id:
            raise InstructionNotFoundError(key)
        if record.owner_id is None:
            await action.service.delete_system(key, InstructionType.MEMORY)
        else:
            await action.service.delete(record.owner_id, record.id)
    except InstructionNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("memory.delete", action.operator, key))
    return DELETED_STATUS


@router.get("/summaries")
async def summaries(read_model: ReadModelDep, page_query: PageDep) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await read_model.list_summaries(resolved_limit, resolved_offset)
    return page_payload(page, serialize_summary)


@router.get("/exchanges")
async def exchanges(
    services: ListingServicesDep,
    page_query: PageDep,
    filters: ExchangeFilterDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_exchanges(
        ExchangeListing(resolved_limit, resolved_offset, filters.user_id, filters.status)
    )
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_exchange(item, resolved_names))
