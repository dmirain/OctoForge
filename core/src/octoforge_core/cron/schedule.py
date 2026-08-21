"""Timezone-aware cron validation and schedule arithmetic."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from octoforge_core.cron.types import CronJob, CronScheduleError, MissedRuns


def compute_next_fire(schedule: str, timezone: str, base: datetime) -> datetime:
    tz = _load_timezone(timezone)
    _ensure_valid(schedule)
    iterator = croniter(schedule, base.astimezone(tz))
    next_local: datetime = iterator.get_next(datetime)
    return next_local.astimezone(UTC)


def count_missed(request: MissedRuns) -> int:
    """Count schedule slots in `(since, now]`, excluding the current due shot."""
    tz = _load_timezone(request.timezone)
    _ensure_valid(request.schedule)
    iterator = croniter(request.schedule, request.since.astimezone(tz))
    local_now = request.now.astimezone(tz)
    slots = 0
    while True:
        next_local: datetime = iterator.get_next(datetime)
        if next_local > local_now or slots > request.cap:
            break
        slots += 1
    return min(max(slots - 1, 0), request.cap)


def missed_run_count(job: CronJob, now: datetime) -> int:
    """Count firings missed since a job was created or last completed."""
    since = job.last_fire_at if job.last_fire_at is not None else job.created_at
    return count_missed(MissedRuns(job.schedule, job.timezone, since, now))


def _load_timezone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronScheduleError(f"unknown IANA timezone: {timezone!r}") from exc


def _ensure_valid(schedule: str) -> None:
    if not croniter.is_valid(schedule):
        raise CronScheduleError(f"invalid cron expression: {schedule!r}")
