"""Tests for background tasks: stores and task skills."""

import pytest

from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.errors import SkillArgumentsError
from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.tasks.tools import (
    NO_SPAWNER_MESSAGE,
    NO_TASKS_MESSAGE,
    TaskListSkill,
    TaskSpawnSkill,
)

CTX = SkillContext(user_id="user-1", channel="web", dialog_id="dlg-1")
OTHER_CTX = SkillContext(user_id="user-2", channel="web", dialog_id="dlg-2")
PROMPT_CONTENT = "solve 2+2"
SPAWNED_TEXT = "task abc123 spawned"
REFUSAL_TEXT = "cannot spawn: process limit (5) reached"
TITLE = "research"
OWN_TASK_COUNT = 2
ACTIVE_COUNT = 2


class FakeSpawner:
    """TaskSpawner stub recording spawn calls."""

    def __init__(self, reply: str = SPAWNED_TEXT) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def spawn(self, title: str, prompt: str) -> str:
        self.calls.append((title, prompt))
        return self.reply


def ctx_with(spawner: FakeSpawner, base: SkillContext = CTX) -> SkillContext:
    return SkillContext(
        user_id=base.user_id,
        channel=base.channel,
        dialog_id=base.dialog_id,
        task_spawner=spawner,
    )


def make_task(ctx: SkillContext = CTX, title: str = "t") -> Task:
    return Task(
        dialog_id=ctx.dialog_id,
        user_id=ctx.user_id,
        channel=ctx.channel,
        title=title,
        kind=TaskKind.RUN,
        input={"title": title, "prompt": PROMPT_CONTENT},
    )


async def test_task_spawn_delegates_to_the_context_spawner() -> None:
    spawner = FakeSpawner()
    skill = TaskSpawnSkill()

    output = await skill.execute({"title": TITLE, "prompt": PROMPT_CONTENT}, ctx_with(spawner))

    assert output == SPAWNED_TEXT
    assert spawner.calls == [(TITLE, PROMPT_CONTENT)]


async def test_task_spawn_reports_spawner_refusals() -> None:
    spawner = FakeSpawner(reply=REFUSAL_TEXT)
    skill = TaskSpawnSkill()

    output = await skill.execute({"title": TITLE, "prompt": PROMPT_CONTENT}, ctx_with(spawner))

    assert output == REFUSAL_TEXT


async def test_task_spawn_without_spawner_fails() -> None:
    skill = TaskSpawnSkill()

    with pytest.raises(SkillArgumentsError, match=NO_SPAWNER_MESSAGE):
        await skill.execute({"title": TITLE, "prompt": PROMPT_CONTENT}, CTX)


@pytest.mark.parametrize(
    "arguments",
    [{"prompt": "p"}, {"title": "t"}, {"title": "", "prompt": "p"}],
)
async def test_task_spawn_validates_arguments(arguments: dict[str, str]) -> None:
    skill = TaskSpawnSkill()

    with pytest.raises(SkillArgumentsError):
        await skill.execute(arguments, ctx_with(FakeSpawner()))


async def test_task_list_scoped_by_dialog() -> None:
    store = InMemoryTaskStore()
    await store.add(make_task(title="a"))
    await store.add(make_task(title="b"))
    await store.add(make_task(ctx=OTHER_CTX, title="c"))
    skill = TaskListSkill(store=store)

    own = await skill.execute({}, CTX)
    other = await skill.execute({}, OTHER_CTX)

    assert len(own.splitlines()) == OWN_TASK_COUNT
    assert "a" in own and "b" in own
    assert len(other.splitlines()) == 1
    assert "c" in other


async def test_task_list_shows_cancelled_status() -> None:
    store = InMemoryTaskStore()
    task = make_task()
    await store.add(task)
    await store.cancel(task.id)
    skill = TaskListSkill(store=store)

    output = await skill.execute({}, CTX)

    assert f"[{TaskStatus.CANCELLED.value}]" in output


async def test_task_list_empty() -> None:
    skill = TaskListSkill(store=InMemoryTaskStore())

    assert await skill.execute({}, CTX) == NO_TASKS_MESSAGE


async def test_mark_delivered_sets_flag() -> None:
    store = InMemoryTaskStore()
    task = make_task()
    await store.add(task)

    await store.mark_delivered(task.id)

    assert (await store.get(task.id)).result_delivered is True


async def test_cancel_marks_task_cancelled() -> None:
    store = InMemoryTaskStore()
    task = make_task()
    await store.add(task)

    await store.cancel(task.id)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.CANCELLED
    assert stored.finished_at is not None
    assert stored.finished_at.tzinfo is not None
    assert await store.is_cancelled(task.id)


async def test_cancel_unknown_task_raises() -> None:
    store = InMemoryTaskStore()

    with pytest.raises(TaskNotFoundError):
        await store.cancel("missing")


async def test_is_cancelled_false_for_missing_or_running() -> None:
    store = InMemoryTaskStore()
    task = make_task()
    await store.add(task)
    await store.mark_running(task)

    assert not await store.is_cancelled("missing")
    assert not await store.is_cancelled(task.id)


async def test_count_active_counts_pending_and_running_of_the_dialog() -> None:
    store = InMemoryTaskStore()
    pending = make_task(title="pending")
    running = make_task(title="running")
    done = make_task(title="done")
    cancelled = make_task(title="cancelled")
    other = make_task(ctx=OTHER_CTX, title="other")
    for task in (pending, running, done, cancelled, other):
        await store.add(task)
    await store.mark_running(running)
    await store.mark_done(done, "ok")
    await store.cancel(cancelled.id)

    assert await store.count_active(CTX.dialog_id) == ACTIVE_COUNT
