"""Cron job endpoints, scoped by the trusted X-User-Id header.

All parameters arrive as the query string: the primary caller is the agent
itself through the external_call tool, which renders URLs but cannot send
request bodies. The trusted header stands in until real authentication
(service tokens) arrives.
"""

import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from octoforge_core.cron.api import (
    CronJob,
    CronJobNotFoundError,
    CronScheduleError,
    CronStore,
    compute_next_fire,
)
from octoforge_core.time import utc_now

from octoforge_web.api.schemas import CronJobCreateParams, CronJobResponse
from octoforge_web.deps import get_channel, get_cron_store, get_user_id

router = APIRouter(prefix="/api/cron")

StoreDep = Annotated[CronStore, Depends(get_cron_store)]
UserIdDep = Annotated[str, Depends(get_user_id)]
ChannelDep = Annotated[str, Depends(get_channel)]

JOB_NOT_FOUND_DETAIL = "cron job not found"


@router.post("/jobs", status_code=HTTPStatus.CREATED)
async def create_job(
    user_id: UserIdDep,
    channel: ChannelDep,
    store: StoreDep,
    params: Annotated[CronJobCreateParams, Query()],
) -> CronJobResponse:
    """Create a cron job; the first fire time is computed from now."""
    job = CronJob(
        id=uuid.uuid4().hex,
        user_id=user_id,
        channel=channel,
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
    return _to_response(await store.create(job))


@router.get("/jobs")
async def list_jobs(user_id: UserIdDep, store: StoreDep) -> list[CronJobResponse]:
    """List all cron jobs of the caller."""
    jobs = await store.list_for_user(user_id)
    return [_to_response(job) for job in jobs]


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
    return _to_response(await store.set_enabled(user_id, job_id, False))


@router.post("/jobs/{job_id}/resume")
async def resume_job(user_id: UserIdDep, store: StoreDep, job_id: str) -> CronJobResponse:
    """Re-enable the caller's job; next fire is recomputed from now (no instant catch-up)."""
    job = await _get_owned_job(store, user_id, job_id)
    next_fire_at = _validated_next_fire(job.schedule, job.timezone)
    return _to_response(await store.set_enabled(user_id, job_id, True, next_fire_at))


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


def _to_response(job: CronJob) -> CronJobResponse:
    return CronJobResponse(
        id=job.id,
        user_id=job.user_id,
        channel=job.channel,
        title=job.title,
        schedule=job.schedule,
        timezone=job.timezone,
        prompt=job.prompt,
        enabled=job.enabled,
        next_fire_at=job.next_fire_at,
        last_fire_at=job.last_fire_at,
        created_at=job.created_at,
        one_shot=job.one_shot,
        last_status=None if job.last_status is None else job.last_status.value,
        last_error=job.last_error,
        retry_count=job.retry_count,
    )
