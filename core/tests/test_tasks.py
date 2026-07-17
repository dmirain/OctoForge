"""Tests for background tasks: store, runner, task skills."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.llm.events import StreamEvent
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.basic.task_list import NO_TASKS_MESSAGE, TaskListSkill
from octoforge_core.skills.basic.task_spawn import TaskSpawnSkill
from octoforge_core.skills.errors import SkillArgumentsError
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.runner import TaskRunner
from octoforge_core.tasks.store import InMemoryTaskStore

CTX = SkillContext(conversation_id="conv-1")
OTHER_CTX = SkillContext(conversation_id="conv-2")
PROMPT_CONTENT = "solve 2+2"
SOLVED = "4"
FAILURE_MESSAGE = "llm down"
TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.05
OWN_TASK_COUNT = 2


class PromptLLM:
    """LLMClient stub solving prompt tasks."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        return ChatMessage(role=MessageRole.ASSISTANT, content=SOLVED)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


class FailingLLM:
    """LLMClient stub failing prompt tasks."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        raise RuntimeError(FAILURE_MESSAGE)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


async def wait_for_status(store: InMemoryTaskStore, task_id: str, status: TaskStatus) -> Task:
    async def _wait() -> Task:
        while True:
            task = await store.get(task_id)
            if task.status is status:
                return task
            await asyncio.sleep(POLL_SECONDS)

    return await asyncio.wait_for(_wait(), timeout=TIMEOUT_SECONDS)


async def test_task_spawn_creates_pending_task() -> None:
    store = InMemoryTaskStore()
    skill = TaskSpawnSkill(store=store)

    output = await skill.execute({"title": "t", "prompt": PROMPT_CONTENT}, CTX)

    tasks = await store.list(CTX.conversation_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status is TaskStatus.PENDING
    assert task.kind is TaskKind.PROMPT
    assert task.input == {"prompt": PROMPT_CONTENT}
    assert task.id in output
    assert task.created_at.tzinfo is not None


@pytest.mark.parametrize(
    "arguments",
    [{"prompt": "p"}, {"title": "t"}, {"title": "", "prompt": "p"}],
)
async def test_task_spawn_validates_arguments(arguments: dict[str, str]) -> None:
    skill = TaskSpawnSkill(store=InMemoryTaskStore())

    with pytest.raises(SkillArgumentsError):
        await skill.execute(arguments, CTX)


async def test_runner_executes_prompt_task_and_notifies() -> None:
    store = InMemoryTaskStore()
    done: list[Task] = []

    async def on_done(task: Task) -> None:
        done.append(task)

    runner = TaskRunner(
        store=store,
        llm_client=PromptLLM(),
        registry=SkillRegistry(),
        on_task_done=on_done,
        poll_interval_seconds=POLL_SECONDS,
    )
    task = Task(
        conversation_id=CTX.conversation_id,
        title="t",
        kind=TaskKind.PROMPT,
        input={"prompt": PROMPT_CONTENT},
    )
    await store.add(task)
    worker = asyncio.create_task(runner.run_forever())
    try:
        stored = await wait_for_status(store, task.id, TaskStatus.DONE)
    finally:
        worker.cancel()

    assert stored.result == SOLVED
    assert stored.finished_at is not None
    assert stored.finished_at.tzinfo is not None
    assert [t.id for t in done] == [task.id]


async def test_runner_marks_task_failed() -> None:
    store = InMemoryTaskStore()

    async def on_done(task: Task) -> None:
        pass

    runner = TaskRunner(
        store=store,
        llm_client=FailingLLM(),
        registry=SkillRegistry(),
        on_task_done=on_done,
        poll_interval_seconds=POLL_SECONDS,
    )
    task = Task(
        conversation_id=CTX.conversation_id,
        title="t",
        kind=TaskKind.PROMPT,
        input={"prompt": PROMPT_CONTENT},
    )
    await store.add(task)
    worker = asyncio.create_task(runner.run_forever())
    try:
        stored = await wait_for_status(store, task.id, TaskStatus.FAILED)
    finally:
        worker.cancel()

    assert stored.error == FAILURE_MESSAGE


async def test_task_list_scoped_by_conversation() -> None:
    store = InMemoryTaskStore()
    await store.add(
        Task(conversation_id=CTX.conversation_id, title="a", kind=TaskKind.PROMPT, input={})
    )
    await store.add(
        Task(conversation_id=CTX.conversation_id, title="b", kind=TaskKind.PROMPT, input={})
    )
    await store.add(
        Task(conversation_id=OTHER_CTX.conversation_id, title="c", kind=TaskKind.PROMPT, input={})
    )
    skill = TaskListSkill(store=store)

    own = await skill.execute({}, CTX)
    other = await skill.execute({}, OTHER_CTX)

    assert len(own.splitlines()) == OWN_TASK_COUNT
    assert "a" in own and "b" in own
    assert len(other.splitlines()) == 1
    assert "c" in other


async def test_task_list_empty() -> None:
    skill = TaskListSkill(store=InMemoryTaskStore())

    assert await skill.execute({}, CTX) == NO_TASKS_MESSAGE
