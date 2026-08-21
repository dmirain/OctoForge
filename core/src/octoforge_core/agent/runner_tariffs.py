"""Deduplicated user notices for tariff-limit refusals."""

from typing import TYPE_CHECKING

from octoforge_core.tariffs.api import LimitVerdict
from octoforge_core.time import utc_now

from .runner_constants import TARIFF_CRON_LIMIT_NOTICE_TEMPLATE

if TYPE_CHECKING:
    from .runner import ConversationRunner


class TariffNotices:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def publish_cron(self, title: str, cron_job_id: str, verdict: LimitVerdict) -> None:
        await self.once(
            cron_job_id,
            TARIFF_CRON_LIMIT_NOTICE_TEMPLATE.format(
                title=title,
                reason=verdict.reason,
                used=verdict.used,
                limit=verdict.limit,
            ),
        )

    async def once(self, key: str, notice: str) -> None:
        day = utc_now().strftime("%Y-%m-%d")
        if self._runner._runtime.tariff_notes.get(key) == day:
            return
        self._runner._runtime.tariff_notes[key] = day
        await self._runner._deliver_notice(notice)
