"""Cron records, commands, outcomes, and domain errors."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from octoforge_core.tasks.api import TaskStatus

MISSED_COUNT_DEFAULT_CAP = 100


class CronJobNotFoundError(Exception):
    """No cron job matches the requested identity and owner."""


class CronScheduleError(Exception):
    """A cron expression or IANA timezone is invalid."""


@dataclass(frozen=True, slots=True)
class CronJob:
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


@dataclass(frozen=True, slots=True)
class CronJobDraft:
    user_id: str
    channel: str
    title: str
    schedule: str
    prompt: str
    timezone: str
    one_shot: bool


@dataclass(frozen=True, slots=True)
class CronEnablement:
    user_id: str
    job_id: str
    enabled: bool
    next_fire_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CronClaim:
    job_id: str
    expected_next_fire_at: datetime
    owner: str
    now: datetime
    stale_before: datetime


@dataclass(frozen=True, slots=True)
class CronFireResult:
    job_id: str
    status: TaskStatus
    error: str | None
    retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class CronWake:
    user_id: str
    channel: str
    title: str
    prompt: str
    cron_job_id: str


@dataclass(frozen=True, slots=True)
class MissedRuns:
    schedule: str
    timezone: str
    since: datetime
    now: datetime
    cap: int = MISSED_COUNT_DEFAULT_CAP


class WakeOutcome(StrEnum):
    DELIVERED = "delivered"
    LIMITED = "limited"
    NOT_OURS = "not_ours"
