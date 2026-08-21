"""Persistence, delivery, and scheduling ports of the cron module."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from octoforge_core.cron.types import (
    CronClaim,
    CronEnablement,
    CronFireResult,
    CronJob,
    CronWake,
    WakeOutcome,
)


class CronStore(Protocol):
    async def create(self, job: CronJob) -> CronJob: ...

    async def get(self, job_id: str) -> CronJob: ...

    async def list_for_user(self, user_id: str) -> list[CronJob]: ...

    async def delete_for_user(self, user_id: str, job_id: str) -> None: ...

    async def set_enabled(self, request: CronEnablement) -> CronJob: ...

    async def list_due(
        self, now: datetime, stale_before: datetime, limit: int
    ) -> list[CronJob]: ...

    async def claim(self, request: CronClaim) -> bool: ...

    async def release_claim(self, job_id: str) -> None: ...

    async def complete_fire(
        self, job_id: str, fired_at: datetime, next_fire_at: datetime
    ) -> None: ...

    async def record_fire_result(self, result: CronFireResult) -> None: ...


class CronWaker(Protocol):
    async def wake(self, request: CronWake) -> WakeOutcome: ...


@runtime_checkable
class Scheduler(Protocol):
    async def run_forever(self) -> None: ...
