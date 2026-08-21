"""The task_create tool: immediate background work or a cron job."""

from typing import Any

from octoforge_core.cron.api import CronJobDraft, CronStore, create_job
from octoforge_core.tariffs.api import LimitGate
from octoforge_core.tasks._tool_args import non_empty_string
from octoforge_core.tasks.tool_contract import (
    CREATE_DESCRIPTION,
    CREATE_NAME,
    CREATE_SCHEMA,
    DEFAULT_TIMEZONE,
    NO_SPAWNER_MESSAGE,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class TaskCreateTool:
    """Create immediate background work or a scheduled cron job."""

    def __init__(self, cron_store: CronStore, limits: LimitGate | None = None) -> None:
        self._cron_store = cron_store
        self._limits = limits

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=CREATE_NAME,
            description=CREATE_DESCRIPTION,
            parameters_schema=CREATE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        title = non_empty_string(arguments.get("title"), "title")
        prompt = non_empty_string(arguments.get("prompt"), "prompt")
        schedule = arguments.get("schedule")
        if schedule is None:
            if arguments.get("one_shot") is True:
                raise ToolArgumentsError("one_shot requires a schedule")
            if context.task_spawner is None:
                raise ToolArgumentsError(NO_SPAWNER_MESSAGE)
            return await context.task_spawner.spawn(title, prompt)
        draft = CronJobDraft(
            user_id=context.user_id,
            channel=context.channel,
            title=title,
            schedule=non_empty_string(schedule, "schedule"),
            prompt=prompt,
            timezone=non_empty_string(arguments.get("timezone") or DEFAULT_TIMEZONE, "timezone"),
            one_shot=arguments.get("one_shot") is True,
        )
        max_jobs = (
            await self._limits.max_cron_jobs(context.user_id) if self._limits is not None else None
        )
        return await create_job(self._cron_store, draft, max_jobs=max_jobs)
