"""Cron job administration."""

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, HTTPException
from octoforge_core.admin.api import clamp_page
from octoforge_core.cron.api import CronEnablement, CronJobNotFoundError
from octoforge_server import audit

from .actions import CronActionDep, ListingServicesDep
from .common import DELETED_STATUS, names, page_payload
from .query import PageDep
from .serializers import cron as serialize_cron

router = APIRouter()


@router.get("/cron")
async def cron_jobs(
    services: ListingServicesDep,
    page_query: PageDep,
) -> dict[str, Any]:
    resolved_limit, resolved_offset = clamp_page(page_query.limit, page_query.offset)
    page = await services.read_model.list_cron_jobs(resolved_limit, resolved_offset)
    resolved_names = await names(services.identities)
    return page_payload(page, lambda item: serialize_cron(item, resolved_names))


@router.post("/cron/{job_id}/enabled")
async def set_cron_enabled(job_id: str, enabled: bool, action: CronActionDep) -> dict[str, Any]:
    try:
        job = await action.store.get(job_id)
        await action.store.set_enabled(CronEnablement(job.user_id, job_id, enabled))
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record(
        audit.AuditEvent(
            "cron.enabled" if enabled else "cron.paused",
            action.operator,
            job_id,
        )
    )
    return {"id": job_id, "enabled": enabled}


@router.delete("/cron/{job_id}")
async def delete_cron_job(job_id: str, action: CronActionDep) -> dict[str, str]:
    try:
        job = await action.store.get(job_id)
        await action.store.delete_for_user(job.user_id, job_id)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    audit.record(audit.AuditEvent("cron.delete", action.operator, job_id))
    return DELETED_STATUS
