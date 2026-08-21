"""Cron job endpoints scoped by the trusted user and channel headers."""

import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from octoforge_core.cron.api import (
    CronEnablement,
    CronJob,
    CronJobNotFoundError,
    CronScheduleError,
    CronStore,
    compute_next_fire,
    job_quota_refusal,
)
from octoforge_core.time import utc_now

from octoforge_server.api.cron_deps import CronActorDep, CronServicesDep
from octoforge_server.api.cron_response import cron_response
from octoforge_server.api.schemas import CronJobCreateParams, CronJobResponse
from octoforge_server.deps import get_cron_store, get_user_id

router = APIRouter(prefix="/api/cron")

StoreDep = Annotated[CronStore, Depends(get_cron_store)]
UserIdDep = Annotated[str, Depends(get_user_id)]

JOB_NOT_FOUND_DETAIL = "cron job not found"


@router.post("/jobs", status_code=HTTPStatus.CREATED)
async def create_job(
    actor: CronActorDep,
    services: CronServicesDep,
    params: Annotated[CronJobCreateParams, Query()],
) -> CronJobResponse:
    """Create a cron job; the first fire time is computed from now.

    The plan's job cap is the same rule the agent tool enforces — the shared
    `job_quota_refusal` keeps this entrance from being a bypass.
    """
    max_jobs = await services.gate.max_cron_jobs(actor.user_id)
    if max_jobs is not None:
        existing = await services.store.list_for_user(actor.user_id)
        refusal = job_quota_refusal(len(existing), max_jobs)
        if refusal is not None:
            raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=refusal)
    job = CronJob(
        id=uuid.uuid4().hex,
        user_id=actor.user_id,
        channel=actor.channel,
        title=params.title,
        schedule=params.schedule,
        timezone=params.timezone,
        prompt=params.prompt,
        enabled=True,
        next_fire_at=_validated_next_fire(params.schedule, params.timezone),
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=utc_now(),
        one_shot=params.one_shot,
        last_status=None,
        last_error=None,
        retry_count=0,
    )
    return cron_response(await services.store.create(job))


@router.get("/jobs")
async def list_jobs(user_id: UserIdDep, store: StoreDep) -> list[CronJobResponse]:
    """List all cron jobs of the caller."""
    jobs = await store.list_for_user(user_id)
    return [cron_response(job) for job in jobs]


@router.delete("/jobs/{job_id}", status_code=HTTPStatus.NO_CONTENT)
async def delete_job(user_id: UserIdDep, store: StoreDep, job_id: str) -> None:
    """Delete the caller's job; foreign and missing ids both answer 404."""
    try:
        await store.delete_for_user(user_id, job_id)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=JOB_NOT_FOUND_DETAIL) from exc


@router.post("/jobs/{job_id}/pause")
async def pause_job(user_id: UserIdDep, store: StoreDep, job_id: str) -> CronJobResponse:
    """Disable the caller's job; it stays in the list but never fires."""
    await _get_owned_job(store, user_id, job_id)
    return cron_response(await store.set_enabled(CronEnablement(user_id, job_id, False)))


@router.post("/jobs/{job_id}/resume")
async def resume_job(user_id: UserIdDep, store: StoreDep, job_id: str) -> CronJobResponse:
    """Re-enable the caller's job; next fire is recomputed from now (no instant catch-up)."""
    job = await _get_owned_job(store, user_id, job_id)
    next_fire_at = _validated_next_fire(job.schedule, job.timezone)
    return cron_response(
        await store.set_enabled(CronEnablement(user_id, job_id, True, next_fire_at))
    )


async def _get_owned_job(store: CronStore, user_id: str, job_id: str) -> CronJob:
    try:
        job = await store.get(job_id)
    except CronJobNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=JOB_NOT_FOUND_DETAIL) from exc
    if job.user_id != user_id:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=JOB_NOT_FOUND_DETAIL)
    return job


def _validated_next_fire(schedule: str, timezone: str) -> datetime:
    try:
        return compute_next_fire(schedule, timezone, utc_now())
    except CronScheduleError as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
