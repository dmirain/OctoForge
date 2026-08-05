"""Tests for deferred work: the task store and the unified task tools."""

from dataclasses import replace
from datetime import timedelta

import pytest

from octoforge_core.cron.api import CronJob, CronJobNotFoundError
from octoforge_core.tariffs.api import LimitVerdict, UsageEvent
from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.tasks.tools import (
    NO_SPAWNER_MESSAGE,
    NO_WORK_MESSAGE,
    TaskCreateTool,
    TaskDeleteTool,
    TaskListTool,
)
from octoforge_core.time import utc_now
from octoforge_core.tools.base import TaskDeleteOutcome, ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

CTX = ToolContext(user_id="user-1", channel="web", dialog_id="dlg-1")
OTHER_CTX = ToolContext(user_id="user-2", channel="web", dialog_id="dlg-2")
PROMPT_CONTENT = "solve 2+2"
SPAWNED_TEXT = "task abc123 spawned"
REFUSAL_TEXT = "cannot spawn: process limit (5) reached"
TITLE = "research"
VALID_SCHEDULE = "0 9 * * *"
VALID_TIMEZONE = "Europe/Moscow"
EXPECTED_TWO_JOBS = 2
RETRY_TWO = 2
PROMPT_WORDS_BEYOND_PREVIEW = 50


class FakeSpawner:
    """TaskSpawner stub recording spawn calls."""

    def __init__(self, reply: str = SPAWNED_TEXT) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def spawn(self, title: str, prompt: str) -> str:
        self.calls.append((title, prompt))
        return self.reply


class FakeDeleter:
    """TaskDeleter stub with a programmed outcome."""

    def __init__(self, outcome: TaskDeleteOutcome = TaskDeleteOutcome.DELETED) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    async def delete(self, task_id: str) -> TaskDeleteOutcome:
        self.calls.append(task_id)
        return self.outcome


class FakeCronStore:
    """In-memory CronStore; only the methods the task tools use are real."""

    def __init__(self) -> None:
        self.jobs: dict[str, CronJob] = {}

    async def create(self, job: CronJob) -> CronJob:
        self.jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> CronJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise CronJobNotFoundError(job_id) from exc

    async def list_for_user(self, user_id: str) -> list[CronJob]:
        return [job for job in self.jobs.values() if job.user_id == user_id]

    async def delete_for_user(self, user_id: str, job_id: str) -> None:
        job = await self.get(job_id)
        if job.user_id != user_id:
            raise CronJobNotFoundError(job_id)
        del self.jobs[job_id]


def ctx_with(
    spawner: FakeSpawner | None = None,
    deleter: FakeDeleter | None = None,
    owner_task_id: str | None = None,
    base: ToolContext = CTX,
) -> ToolContext:
    return ToolContext(
        user_id=base.user_id,
        channel=base.channel,
        dialog_id=base.dialog_id,
        task_spawner=spawner,
        task_deleter=deleter,
        owner_task_id=owner_task_id,
    )


def make_task(ctx: ToolContext = CTX, title: str = "t", **overrides: object) -> Task:
    task = Task(
        dialog_id=ctx.dialog_id,
        title=title,
        kind=TaskKind.RUN,
        input={"title": title, "prompt": PROMPT_CONTENT},
    )
    return replace(task, **overrides) if overrides else task


def make_cron_job(user_id: str = CTX.user_id, **overrides: object) -> CronJob:
    base = CronJob(
        id="job-1",
        user_id=user_id,
        channel=CTX.channel,
        title="morning report",
        schedule=VALID_SCHEDULE,
        timezone=VALID_TIMEZONE,
        prompt="good morning",
        enabled=True,
        next_fire_at=utc_now() + timedelta(hours=1),
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=utc_now(),
        one_shot=False,
        last_status=None,
        last_error=None,
        retry_count=0,
    )
    return replace(base, **overrides) if overrides else base


def schedule_arguments(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {"title": TITLE, "prompt": PROMPT_CONTENT}
    return arguments | {"schedule": VALID_SCHEDULE, "timezone": VALID_TIMEZONE} | overrides


# --- task_create: immediate path -------------------------------------------


async def test_task_create_without_schedule_delegates_to_the_spawner() -> None:
    spawner = FakeSpawner()
    tool = TaskCreateTool(FakeCronStore())

    output = await tool.execute({"title": TITLE, "prompt": PROMPT_CONTENT}, ctx_with(spawner))

    assert output == SPAWNED_TEXT
    assert spawner.calls == [(TITLE, PROMPT_CONTENT)]


async def test_task_create_reports_spawner_refusals() -> None:
    tool = TaskCreateTool(FakeCronStore())

    output = await tool.execute(
        {"title": TITLE, "prompt": PROMPT_CONTENT}, ctx_with(FakeSpawner(reply=REFUSAL_TEXT))
    )

    assert output == REFUSAL_TEXT


async def test_task_create_without_spawner_fails() -> None:
    tool = TaskCreateTool(FakeCronStore())

    with pytest.raises(ToolArgumentsError, match=NO_SPAWNER_MESSAGE):
        await tool.execute({"title": TITLE, "prompt": PROMPT_CONTENT}, CTX)


async def test_task_create_one_shot_without_schedule_fails() -> None:
    tool = TaskCreateTool(FakeCronStore())

    with pytest.raises(ToolArgumentsError, match="one_shot"):
        await tool.execute(
            {"title": TITLE, "prompt": PROMPT_CONTENT, "one_shot": True},
            ctx_with(FakeSpawner()),
        )


@pytest.mark.parametrize(
    "arguments",
    [{"prompt": "p"}, {"title": "t"}, {"title": "", "prompt": "p"}],
)
async def test_task_create_validates_arguments(arguments: dict[str, str]) -> None:
    tool = TaskCreateTool(FakeCronStore())

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, ctx_with(FakeSpawner()))


# --- task_create: schedule path --------------------------------------------


async def test_task_create_with_schedule_stores_an_enabled_job() -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store)

    result = await tool.execute(schedule_arguments(), CTX)

    assert "created cron job" in result
    (job,) = store.jobs.values()
    assert job.user_id == CTX.user_id
    assert job.channel == CTX.channel
    assert job.timezone == VALID_TIMEZONE
    assert job.enabled
    assert job.next_fire_at > utc_now()
    assert job.id in result


async def test_task_create_with_schedule_defaults_the_timezone_to_utc() -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store)

    await tool.execute({"title": TITLE, "prompt": PROMPT_CONTENT, "schedule": VALID_SCHEDULE}, CTX)

    (job,) = store.jobs.values()
    assert job.timezone == "UTC"


async def test_task_create_with_schedule_marks_one_shot_jobs() -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store)

    result = await tool.execute(schedule_arguments(one_shot=True), CTX)

    (job,) = store.jobs.values()
    assert job.one_shot
    assert "one-shot" in result


@pytest.mark.parametrize(
    "overrides",
    [{"schedule": "not a cron"}, {"timezone": "Mars/Olympus"}],
)
async def test_task_create_with_schedule_rejects_bad_input(overrides: dict[str, str]) -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store)

    result = await tool.execute(schedule_arguments(**overrides), CTX)

    assert result.startswith("error:")
    assert store.jobs == {}


async def test_task_create_with_schedule_is_idempotent_for_an_identical_job() -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store)

    first = await tool.execute(schedule_arguments(), CTX)
    second = await tool.execute(schedule_arguments(), CTX)

    assert "created cron job" in first
    assert "already exists" in second
    assert len(store.jobs) == 1


class CapGate:
    """LimitGate stub with a configurable cron-job cap; everything else open."""

    def __init__(self, max_cron_jobs: int | None) -> None:
        self._max_cron_jobs = max_cron_jobs

    async def enabled_features(self, user_id: str) -> frozenset[str] | None:
        return None

    async def allows(self, user_id: str, feature: str) -> bool:
        return True

    async def check_submit(self, user_id: str) -> LimitVerdict:
        return LimitVerdict.ok()

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        return LimitVerdict.ok()

    async def max_cron_jobs(self, user_id: str) -> int | None:
        return self._max_cron_jobs

    async def max_datasets(self, user_id: str) -> int | None:
        return None

    async def record(self, event: UsageEvent) -> None:
        return None


async def test_task_create_refuses_over_the_plans_job_cap() -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store, limits=CapGate(max_cron_jobs=1))

    first = await tool.execute(schedule_arguments(), CTX)
    second = await tool.execute(schedule_arguments(title="evening report"), CTX)

    assert "created cron job" in first
    assert "at most 1 scheduled jobs" in second
    assert len(store.jobs) == 1


async def test_a_duplicate_job_is_never_refused_by_the_cap() -> None:
    """Idempotence beats the quota: the identical job answers with itself."""
    store = FakeCronStore()
    tool = TaskCreateTool(store, limits=CapGate(max_cron_jobs=1))

    await tool.execute(schedule_arguments(), CTX)
    repeat = await tool.execute(schedule_arguments(), CTX)

    assert "already exists" in repeat
    assert len(store.jobs) == 1


async def test_task_create_with_a_different_one_shot_is_not_a_duplicate() -> None:
    store = FakeCronStore()
    tool = TaskCreateTool(store)

    await tool.execute(schedule_arguments(), CTX)
    result = await tool.execute(schedule_arguments(one_shot=True), CTX)

    assert "created cron job" in result
    assert len(store.jobs) == EXPECTED_TWO_JOBS


# --- task_list --------------------------------------------------------------


async def test_task_list_shows_tasks_and_jobs_in_two_sections() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task(title="a"))
    await store.add(make_task(ctx=OTHER_CTX, title="foreign"))
    cron = FakeCronStore()
    await cron.create(make_cron_job())
    await cron.create(make_cron_job(id="job-2", user_id=OTHER_CTX.user_id))
    tool = TaskListTool(store=store, cron_store=cron)

    output = await tool.execute({}, CTX)

    tasks_section, jobs_section = output.split("\n\n")
    assert tasks_section.startswith("background tasks:")
    assert "a" in tasks_section and "foreign" not in tasks_section
    assert f"prompt: {PROMPT_CONTENT!r}" in tasks_section
    assert jobs_section.startswith("scheduled jobs:")
    assert "job-1" in jobs_section and "job-2" not in jobs_section
    assert "[enabled]" in jobs_section
    assert "prompt: 'good morning'" in jobs_section


async def test_task_list_hides_answer_kind_tasks() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task(title="visible"))
    await store.add(make_task(title="internal", kind=TaskKind.ANSWER))
    tool = TaskListTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({}, CTX)

    tasks_section = output.split("\n\n")[0]
    assert "visible" in tasks_section
    assert "internal" not in tasks_section


async def test_task_list_hides_finished_history() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task(title="alpha", status=TaskStatus.RUNNING))
    await store.add(make_task(title="bravo", status=TaskStatus.DONE, result="r"))
    await store.add(
        make_task(title="charlie", status=TaskStatus.DONE, result="r", delivered_at=utc_now())
    )
    await store.add(make_task(title="delta", status=TaskStatus.CANCELLED))
    tool = TaskListTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({}, CTX)

    tasks_section = output.split("\n\n")[0]
    assert "alpha" in tasks_section
    assert "bravo" in tasks_section
    assert "charlie" not in tasks_section
    assert "delta" not in tasks_section


async def test_task_list_truncates_long_prompts() -> None:
    long_prompt = "word " * PROMPT_WORDS_BEYOND_PREVIEW
    store = InMemoryTaskStore()
    await store.add(make_task(input={"title": "t", "prompt": long_prompt}))
    cron = FakeCronStore()
    await cron.create(make_cron_job(prompt=long_prompt))
    tool = TaskListTool(store=store, cron_store=cron)

    output = await tool.execute({}, CTX)

    tasks_section, jobs_section = output.split("\n\n")
    assert "…" in tasks_section and "…" in jobs_section
    assert long_prompt not in output


async def test_task_list_skips_a_missing_task_prompt() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task(input={"title": "t"}))
    tool = TaskListTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({}, CTX)

    assert "prompt:" not in output.split("\n\n")[0]


async def test_task_list_reports_empty_sections() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task())
    tool = TaskListTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({}, CTX)

    assert "no cron jobs" in output
    tool = TaskListTool(store=InMemoryTaskStore(), cron_store=FakeCronStore())
    assert "no tasks" in await tool.execute({}, CTX)


async def test_task_list_empty() -> None:
    tool = TaskListTool(store=InMemoryTaskStore(), cron_store=FakeCronStore())

    assert await tool.execute({}, CTX) == NO_WORK_MESSAGE


async def test_task_list_shows_the_last_run_and_retry_streak() -> None:
    cron = FakeCronStore()
    await cron.create(
        make_cron_job(
            last_status=TaskStatus.FAILED,
            last_error="iteration limit reached",
            retry_count=RETRY_TWO,
        )
    )
    tool = TaskListTool(store=InMemoryTaskStore(), cron_store=cron)

    output = await tool.execute({}, CTX)

    assert "last run: failed (iteration limit reached)" in output
    assert "retry #2" in output


# --- task_delete ------------------------------------------------------------


async def test_task_delete_keeps_a_terminal_task_row() -> None:
    store = InMemoryTaskStore()
    task = make_task(status=TaskStatus.DONE, result="ok")
    await store.add(task)
    tool = TaskDeleteTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({"task_id": task.id}, CTX)

    assert output == f"stopped task {task.id}"
    assert (await store.get(task.id)).status is TaskStatus.DONE


async def test_task_delete_stops_a_running_task_via_the_deleter() -> None:
    store = InMemoryTaskStore()
    task = make_task(status=TaskStatus.RUNNING)
    await store.add(task)
    deleter = FakeDeleter()
    tool = TaskDeleteTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({"task_id": task.id}, ctx_with(deleter=deleter))

    assert output == f"stopped task {task.id}"
    assert deleter.calls == [task.id]
    # the stopped process's finalization marks the row CANCELLED; the tool keeps it
    assert (await store.get(task.id)).status is TaskStatus.RUNNING


async def test_task_delete_refuses_self_deletion_from_inside_the_task() -> None:
    store = InMemoryTaskStore()
    task = make_task(status=TaskStatus.RUNNING)
    await store.add(task)
    deleter = FakeDeleter()
    tool = TaskDeleteTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute(
        {"task_id": task.id}, ctx_with(deleter=deleter, owner_task_id=task.id)
    )

    assert output.startswith("error:")
    assert deleter.calls == []
    assert (await store.get(task.id)).status is TaskStatus.RUNNING


async def test_task_delete_cancels_an_orphaned_running_row() -> None:
    store = InMemoryTaskStore()
    task = make_task(status=TaskStatus.RUNNING)
    await store.add(task)
    deleter = FakeDeleter(outcome=TaskDeleteOutcome.NOT_RUNNING)
    tool = TaskDeleteTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({"task_id": task.id}, ctx_with(deleter=deleter))

    assert output == f"stopped task {task.id}"
    assert (await store.get(task.id)).status is TaskStatus.CANCELLED


async def test_task_delete_cancels_a_running_row_without_a_deleter() -> None:
    store = InMemoryTaskStore()
    task = make_task(status=TaskStatus.RUNNING)
    await store.add(task)
    tool = TaskDeleteTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({"task_id": task.id}, CTX)

    assert output == f"stopped task {task.id}"
    assert (await store.get(task.id)).status is TaskStatus.CANCELLED


async def test_task_delete_scopes_tasks_to_the_dialog() -> None:
    store = InMemoryTaskStore()
    task = make_task(ctx=OTHER_CTX)
    await store.add(task)
    tool = TaskDeleteTool(store=store, cron_store=FakeCronStore())

    output = await tool.execute({"task_id": task.id}, CTX)

    assert output.startswith("error:")
    assert (await store.get(task.id)).id == task.id


async def test_task_delete_removes_a_cron_job() -> None:
    cron = FakeCronStore()
    await cron.create(make_cron_job())
    tool = TaskDeleteTool(store=InMemoryTaskStore(), cron_store=cron)

    output = await tool.execute({"task_id": "job-1"}, CTX)

    assert output == "deleted cron job job-1"
    assert cron.jobs == {}


async def test_task_delete_refuses_a_foreign_cron_job() -> None:
    cron = FakeCronStore()
    await cron.create(make_cron_job(user_id=OTHER_CTX.user_id))
    tool = TaskDeleteTool(store=InMemoryTaskStore(), cron_store=cron)

    output = await tool.execute({"task_id": "job-1"}, CTX)

    assert output.startswith("error:")
    assert "job-1" in cron.jobs


async def test_task_delete_reports_unknown_ids() -> None:
    tool = TaskDeleteTool(store=InMemoryTaskStore(), cron_store=FakeCronStore())

    output = await tool.execute({"task_id": "missing"}, CTX)

    assert output.startswith("error:")


# --- InMemoryTaskStore ------------------------------------------------------


async def test_store_delete_removes_the_row() -> None:
    store = InMemoryTaskStore()
    task = make_task()
    await store.add(task)

    await store.delete(task.id)

    with pytest.raises(TaskNotFoundError):
        await store.get(task.id)


async def test_store_delete_unknown_task_raises() -> None:
    store = InMemoryTaskStore()

    with pytest.raises(TaskNotFoundError):
        await store.delete("missing")


async def test_list_orphaned_returns_pending_and_running_without_mutation() -> None:
    store = InMemoryTaskStore()
    pending = make_task(title="pending")
    running = make_task(title="running", status=TaskStatus.RUNNING)
    done = make_task(title="done", status=TaskStatus.DONE)
    cancelled = make_task(title="cancelled", status=TaskStatus.CANCELLED)
    for task in (pending, running, done, cancelled):
        await store.add(task)

    orphaned = await store.list_orphaned()

    assert {task.id for task in orphaned} == {pending.id, running.id}
    # read-only: the sweep must not mutate the tasks it returns
    assert (await store.get(pending.id)).status is TaskStatus.PENDING
    assert (await store.get(running.id)).status is TaskStatus.RUNNING


async def test_list_orphaned_without_candidates_returns_empty() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task(status=TaskStatus.DONE))

    assert await store.list_orphaned() == []


async def test_list_undelivered_returns_terminal_tasks_without_delivery_stamp() -> None:
    store = InMemoryTaskStore()
    done = make_task(title="done", status=TaskStatus.DONE)
    failed = make_task(title="failed", status=TaskStatus.FAILED)
    delivered = make_task(title="delivered", status=TaskStatus.DONE)
    running = make_task(title="running", status=TaskStatus.RUNNING)
    for task in (done, failed, delivered, running):
        await store.add(task)
    await store.mark_delivered(delivered.id)

    undelivered = await store.list_undelivered()

    assert {task.id for task in undelivered} == {done.id, failed.id}


async def test_mark_delivered_stamps_the_task() -> None:
    store = InMemoryTaskStore()
    task = make_task(status=TaskStatus.DONE)
    await store.add(task)

    await store.mark_delivered(task.id)

    stored = await store.get(task.id)
    assert stored.delivered_at is not None
    assert stored.delivered_at.tzinfo is not None
    assert await store.list_undelivered() == []


async def test_mark_delivered_unknown_task_raises() -> None:
    store = InMemoryTaskStore()

    with pytest.raises(TaskNotFoundError):
        await store.mark_delivered("missing")
