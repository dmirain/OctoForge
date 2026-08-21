"""Aggregated and raw usage-ledger views."""

from typing import Annotated, Any

from fastapi import APIRouter, Query
from octoforge_core.admin.api import clamp_page

from .actions import ListingServicesDep
from .common import names, page_payload
from .deps import IdentityStoreDep, ReadModelDep
from .query import PageDep
from .tariff_serializers import usage_event as serialize_usage_event

router = APIRouter()
DEFAULT_DAYS = 30
MAX_DAYS = 366


@router.get("/usage")
async def usage_report(
    read_model: ReadModelDep,
    identities: IdentityStoreDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> dict[str, Any]:
    rows = await read_model.usage_report(days)
    resolved_names = await names(identities)
    items = [
        {
            "user_id": row.user_id,
            "user_name": resolved_names.get(row.user_id, ""),
            "kind": row.kind,
            "origin": row.origin,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "quantity": row.quantity,
            "events": row.events,
        }
        for row in rows
    ]
    return {
        "days": days,
        "items": items,
        "total": len(items),
        "limit": max(1, len(items)),
        "offset": 0,
    }


@router.get("/usage/events")
async def usage_events(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_usage_events(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_usage_event(item, resolved_names))
