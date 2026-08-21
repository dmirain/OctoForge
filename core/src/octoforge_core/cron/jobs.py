"""Cron job creation policy and human-readable rendering."""

import uuid

from octoforge_core.cron.ports import CronStore
from octoforge_core.cron.schedule import compute_next_fire
from octoforge_core.cron.tool_contract import (
    DUPLICATE_MESSAGE,
    PROMPT_PREVIEW_CHARS,
    QUOTA_MESSAGE,
)
from octoforge_core.cron.types import CronJob, CronJobDraft, CronScheduleError
from octoforge_core.time import utc_now


async def create_job(store: CronStore, draft: CronJobDraft, max_jobs: int | None = None) -> str:
    try:
        next_fire_at = compute_next_fire(draft.schedule, draft.timezone, utc_now())
    except CronScheduleError as exc:
        return f"error: {exc}"
    existing = await store.list_for_user(draft.user_id)
    duplicate = _find_duplicate(existing, draft)
    if duplicate is not None:
        return DUPLICATE_MESSAGE.format(job_id=duplicate.id) + "\n" + format_job(duplicate)
    refusal = job_quota_refusal(len(existing), max_jobs)
    if refusal is not None:
        return refusal
    job = CronJob(
        id=uuid.uuid4().hex,
        user_id=draft.user_id,
        channel=draft.channel,
        title=draft.title,
        schedule=draft.schedule,
        timezone=draft.timezone,
        prompt=draft.prompt,
        enabled=True,
        next_fire_at=next_fire_at,
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=utc_now(),
        one_shot=draft.one_shot,
        last_status=None,
        last_error=None,
        retry_count=0,
    )
    stored = await store.create(job)
    return f"created cron job {stored.id}\n" + format_job(stored)


def _find_duplicate(existing: list[CronJob], draft: CronJobDraft) -> CronJob | None:
    for job in existing:
        same_identity = job.title == draft.title and job.schedule == draft.schedule
        same_action = job.prompt == draft.prompt and job.one_shot == draft.one_shot
        if same_identity and same_action:
            return job
    return None


def job_quota_refusal(existing_count: int, max_jobs: int | None) -> str | None:
    if max_jobs is None or existing_count < max_jobs:
        return None
    return QUOTA_MESSAGE.format(limit=max_jobs)


def format_job(job: CronJob) -> str:
    state = "enabled" if job.enabled else "paused"
    line = (
        f"{job.id} [{state}] {job.title!r} — {job.schedule} ({job.timezone}), "
        f"next fire at {job.next_fire_at.isoformat()}, prompt: {prompt_preview(job.prompt)!r}"
    )
    if job.one_shot:
        line += ", one-shot"
    if job.last_fire_at is not None:
        line += f", last fire at {job.last_fire_at.isoformat()}"
    if job.last_status is not None:
        line += f", last run: {job.last_status.value}"
        if job.last_error:
            line += f" ({job.last_error})"
    if job.retry_count > 0:
        line += f", retry #{job.retry_count}"
    return line


def prompt_preview(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= PROMPT_PREVIEW_CHARS:
        return one_line
    return one_line[:PROMPT_PREVIEW_CHARS] + "…"
