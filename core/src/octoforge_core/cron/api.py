"""Public boundary of the cron module.

Everything the rest of the system (web API, scheduler, composition root) may
know about cron jobs lives here: the `CronStore` protocol, the `CronWaker`
port, the `Scheduler` engine port, the JSON-serializable DTO, the module
errors and the schedule math (`compute_next_fire`, `count_missed`).

The protocols are deliberately transport-shaped: the DTO contains only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation of `CronStore` is the planned "extract to a
dedicated service" path — call sites will not change.

Alternative scheduling engines (Celery beat, APScheduler, OS cron) plug in
two ways: implement the `Scheduler` port and start it instead of
`CronScheduler`, or don't start ours and drive the public firing contract
from the outside: `CronStore.list_due`/`claim`/`release_claim`/
`complete_fire` plus `compute_next_fire`/`count_missed`. Process outcomes
flow back through `record_fire_result` (called by the dialog side via the
`CronOutcomeReporter` adapter, not by the engine).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from octoforge_core.tasks.api import TaskStatus
from octoforge_core.time import utc_now

MISSED_COUNT_DEFAULT_CAP = 100


class CronJobNotFoundError(Exception):
    """Raised when no cron job matches the requested (owner, id) pair."""


class CronScheduleError(Exception):
    """Raised when a cron expression or an IANA timezone fails validation."""


@dataclass(frozen=True, slots=True)
class CronJob:
    """One scheduled recurring prompt, always owned by a single user.

    JSON-friendly: str/bool fields and UTC datetimes (ISO 8601 at a wire
    boundary). `schedule`/`timezone` define the wall-clock cadence ("morning"
    means the user's local morning); all `*_at` fields are aware UTC.
    `claimed_by`/`claimed_at` form the scheduler lease (exactly-once firing).
    `one_shot` marks a single-fire reminder: the job is deleted after the
    first successful outcome. `last_status`/`last_error` are the outcome of
    the most recent fired process; `retry_count` is the running retry streak
    (reset on success or exhaustion).
    """

    id: str
    user_id: str
    channel: str
    title: str
    schedule: str
    timezone: str
    prompt: str
    enabled: bool
    next_fire_at: datetime
    last_fire_at: datetime | None
    claimed_by: str | None
    claimed_at: datetime | None
    created_at: datetime
    one_shot: bool
    last_status: TaskStatus | None
    last_error: str | None
    retry_count: int


class CronStore(Protocol):
    """Port of the cron module: persistence and lease handling for jobs.

    Implementations: `SqlAlchemyCronStore` (SQL, in-process); a future HTTP
    client implementation for a dedicated cron service. The implementation
    is chosen in the composition root.
    """

    async def create(self, job: CronJob) -> CronJob:
        """Persist a fully populated job; return the stored copy."""
        ...

    async def get(self, job_id: str) -> CronJob:
        """Return the job by id regardless of owner; raise `CronJobNotFoundError`."""
        ...

    async def list_for_user(self, user_id: str) -> list[CronJob]:
        """Return all jobs of this owner, oldest first."""
        ...

    async def delete_for_user(self, user_id: str, job_id: str) -> None:
        """Delete the owner's job; raise `CronJobNotFoundError` for foreign/missing ids."""
        ...

    async def set_enabled(
        self,
        user_id: str,
        job_id: str,
        enabled: bool,
        next_fire_at: datetime | None = None,
    ) -> CronJob:
        """Pause/resume the owner's job, optionally moving `next_fire_at`.

        Raises `CronJobNotFoundError` for foreign/missing ids.
        """
        ...

    async def list_due(self, now: datetime, stale_before: datetime, limit: int) -> list[CronJob]:
        """Return enabled jobs due at `now`, earliest first, capped at `limit`.

        A job is due when `next_fire_at <= now` and it is either unclaimed or
        its claim is stale (`claimed_at < stale_before`, i.e. the lease TTL
        has expired and the previous owner presumably died mid-fire).
        """
        ...

    async def claim(
        self,
        job_id: str,
        expected_next_fire_at: datetime,
        owner: str,
        now: datetime,
        stale_before: datetime,
    ) -> bool:
        """Atomically take the lease; return False when the race was lost.

        A single conditional UPDATE: the row must still sit at
        `expected_next_fire_at` (nobody fired/resumed it since listing) and be
        claimable (unclaimed or stale). The rowcount decides the outcome.
        """
        ...

    async def release_claim(self, job_id: str) -> None:
        """Drop the lease without firing (the wake delivery failed)."""
        ...

    async def complete_fire(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        """Record a successful fire: bump last/next fire times and drop the lease."""
        ...

    async def record_fire_result(
        self,
        job_id: str,
        status: TaskStatus,
        error: str | None,
        retry_at: datetime | None,
    ) -> None:
        """Record the outcome of the fired process (reported by the dialog side).

        Always overwrites `last_status`/`last_error`. When `retry_at` is set
        the job is rescheduled to it and the retry streak grows; otherwise the
        streak resets (success, exhaustion or cancellation — the next schedule
        slot computed by `complete_fire` stays).
        """
        ...


class CronWaker(Protocol):
    """Port the scheduler fires due jobs through: deliver the prompt to a dialog."""

    async def wake(
        self,
        user_id: str,
        channel: str,
        title: str,
        prompt: str,
        cron_job_id: str,
    ) -> bool:
        """Start the job's background process in the user's dialog.

        Returns `False` when the process limit was hit and the job was not
        actually started (a system note is published instead); the caller
        must not advance the job's schedule in that case.
        """
        ...


@runtime_checkable
class Scheduler(Protocol):
    """Port of the cron firing engine: the loop that fires due jobs.

    Implementation shipped with the core: `CronScheduler` (asyncio polling
    with CAS leases). An installer either substitutes its own engine here or
    drives `CronStore` + the schedule math from an external runner directly.
    """

    async def run_forever(self) -> None:
        """Fire due jobs forever; cancellation is the stop signal."""
        ...


def compute_next_fire(schedule: str, timezone: str, base: datetime) -> datetime:
    """Return the first schedule slot strictly after `base`, as aware UTC.

    Raises `CronScheduleError` for an invalid cron expression or timezone.
    """
    tz = _load_timezone(timezone)
    _ensure_valid(schedule)
    iterator = croniter(schedule, base.astimezone(tz))
    next_local: datetime = iterator.get_next(datetime)
    return next_local.astimezone(UTC)


def count_missed(
    schedule: str,
    timezone: str,
    since: datetime,
    now: datetime,
    cap: int = MISSED_COUNT_DEFAULT_CAP,
) -> int:
    """Return how many scheduled runs were missed within (since, now].

    The interval includes the current (due) shot, so the result is the slot
    count minus one, clamped to [0, cap]; iteration stops as soon as the cap
    is known to be reached. Raises `CronScheduleError` like `compute_next_fire`.
    """
    tz = _load_timezone(timezone)
    _ensure_valid(schedule)
    iterator = croniter(schedule, since.astimezone(tz))
    local_now = now.astimezone(tz)
    slots = 0
    while True:
        next_local: datetime = iterator.get_next(datetime)
        if next_local > local_now or slots > cap:
            break
        slots += 1
    return min(max(slots - 1, 0), cap)


def _load_timezone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronScheduleError(f"unknown IANA timezone: {timezone!r}") from exc


def _ensure_valid(schedule: str) -> None:
    if not croniter.is_valid(schedule):
        raise CronScheduleError(f"invalid cron expression: {schedule!r}")


# --- the cross-module boundary of job creation and rendering ------------------
# task_create (tasks/tools.py) creates cron jobs through this facade; the
# schema fragments below are the shared LLM-facing contract of those params.

NO_JOBS_MESSAGE = "no cron jobs"
JOB_NOT_FOUND_MESSAGE = "error: cron job not found"
DELETED_MESSAGE = "deleted cron job {job_id}"
DUPLICATE_MESSAGE = "already exists: cron job {job_id}"
PROMPT_PREVIEW_CHARS = 200

TITLE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Short job name, e.g. 'morning report'",
}
SCHEDULE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "Cron expression, e.g. '0 9 * * *' for daily at 09:00; for a one-shot reminder "
        "include the date fields, e.g. '30 15 21 7 *' for July 21 at 15:30"
    ),
}
PROMPT_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Instruction the agent receives on every firing",
}
TIMEZONE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": 'IANA timezone, e.g. "Europe/Moscow"; use "UTC" if unknown',
}
ONE_SHOT_PARAM: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "true for a one-time reminder: the job fires once and is deleted after "
        "the first successful run; default false (recurring)"
    ),
}


@dataclass(frozen=True, slots=True)
class CronJobDraft:
    """Fields of a cron job to create; id and timestamps are assigned at creation."""

    user_id: str
    channel: str
    title: str
    schedule: str
    prompt: str
    timezone: str
    one_shot: bool


async def create_job(store: CronStore, draft: CronJobDraft) -> str:
    """Create a cron job owned by the user; returns the confirmation text.

    Identical jobs are deduplicated: creating the same job twice returns the
    existing one instead of a second record.
    """
    try:
        next_fire_at = compute_next_fire(draft.schedule, draft.timezone, utc_now())
    except CronScheduleError as exc:
        return f"error: {exc}"
    duplicate = await _find_duplicate(store, draft)
    if duplicate is not None:
        return DUPLICATE_MESSAGE.format(job_id=duplicate.id) + "\n" + format_job(duplicate)
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


async def _find_duplicate(store: CronStore, draft: CronJobDraft) -> CronJob | None:
    """Return the identical existing job, if any (idempotence guard)."""
    for job in await store.list_for_user(draft.user_id):
        if (
            job.title == draft.title
            and job.schedule == draft.schedule
            and job.prompt == draft.prompt
            and job.one_shot == draft.one_shot
        ):
            return job
    return None


def format_job(job: CronJob) -> str:
    state = "enabled" if job.enabled else "paused"
    line = (
        f"{job.id} [{state}] {job.title!r} — {job.schedule} ({job.timezone}), "
        f"next fire at {job.next_fire_at.isoformat()}"
    )
    line += f", prompt: {prompt_preview(job.prompt)!r}"
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
    """One-line prompt preview, truncated to PROMPT_PREVIEW_CHARS with an ellipsis."""
    one_line = " ".join(text.split())
    if len(one_line) <= PROMPT_PREVIEW_CHARS:
        return one_line
    return one_line[:PROMPT_PREVIEW_CHARS] + "…"
