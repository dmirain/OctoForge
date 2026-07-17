"""Per-conversation actor: serializes commands and broadcasts loop events."""

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.errors import ConversationNotFoundError
from octoforge_core.agent.events import Failed, LoopEvent
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.skills.base import SkillContext
from octoforge_core.tasks.models import Task

SUBSCRIBER_QUEUE_SIZE = 100
TASK_DONE_TEMPLATE = (
    "Background task '{title}' has finished with status {status}.\nResult:\n{result}"
)


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A loop event wrapped with conversation metadata."""

    conversation_id: str
    seq: int
    payload: LoopEvent


@dataclass(frozen=True, slots=True)
class _Submit:
    message: ChatMessage


@dataclass(frozen=True, slots=True)
class _Cancel:
    pass


@dataclass(frozen=True, slots=True)
class _TaskDone:
    task: Task


_Command = _Submit | _Cancel | _TaskDone


class ConversationRunner:
    """Actor owning a conversation's history, loop runs and subscribers."""

    def __init__(self, conversation_id: str, loop: AgentLoop, system_prompt: str) -> None:
        self._conversation_id = conversation_id
        self._loop = loop
        self._system_prompt = system_prompt
        self._history: list[ChatMessage] = []
        self._inbox: asyncio.Queue[_Command] = asyncio.Queue()
        self._subscribers: set[asyncio.Queue[ConversationEvent]] = set()
        self._seq = 0
        self._control: LoopControl | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._actor_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start consuming the inbox."""
        if self._actor_task is None:
            self._actor_task = asyncio.create_task(self._run_actor())

    async def stop(self) -> None:
        """Cancel the current run and the actor itself."""
        self._handle_cancel()
        if self._actor_task is not None:
            self._actor_task.cancel()

    async def submit(self, content: str) -> None:
        """Submit a user message; injected mid-run or starts a new run."""
        await self._inbox.put(_Submit(ChatMessage(role=MessageRole.USER, content=content)))

    async def cancel(self) -> None:
        """Cancel the current run, if any."""
        await self._inbox.put(_Cancel())

    async def notify_task_done(self, task: Task) -> None:
        """Deliver a finished background task to the conversation."""
        await self._inbox.put(_TaskDone(task))

    def subscribe(self) -> asyncio.Queue[ConversationEvent]:
        """Attach a subscriber queue receiving broadcast events."""
        queue: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ConversationEvent]) -> None:
        """Detach a subscriber queue."""
        self._subscribers.discard(queue)

    def history(self) -> list[ChatMessage]:
        """Return a copy of the conversation history."""
        return list(self._history)

    async def _run_actor(self) -> None:
        while True:
            command = await self._inbox.get()
            if isinstance(command, _Submit):
                self._handle_submit(command.message)
            elif isinstance(command, _Cancel):
                self._handle_cancel()
            elif isinstance(command, _TaskDone):
                self._handle_task_done(command.task)

    def _is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def _handle_submit(self, message: ChatMessage) -> None:
        if self._is_running() and self._control is not None:
            self._control.inject(message)
            return
        self._history.append(message)
        self._start_run()

    def _handle_cancel(self) -> None:
        if self._control is not None:
            self._control.cancel()

    def _handle_task_done(self, task: Task) -> None:
        notification = ChatMessage(role=MessageRole.SYSTEM, content=self._format_task_done(task))
        if self._is_running() and self._control is not None:
            self._control.inject(notification)
            return
        self._history.append(notification)
        self._start_run()

    def _start_run(self) -> None:
        control = LoopControl()
        self._control = control
        self._run_task = asyncio.create_task(self._pump(control))

    async def _pump(self, control: LoopControl) -> None:
        context = SkillContext(conversation_id=self._conversation_id)
        old_size = len(self._history)
        working = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._system_prompt),
            *self._history,
        ]
        try:
            async for event in self._loop.stream(working, control, context):
                self._broadcast(event)
        except Exception as exc:  # loop failures are broadcast, not raised
            self._broadcast(Failed(error=str(exc)))
        finally:
            self._history.extend(working[old_size + 1 :])
            self._control = None
            for leftover in control.drain():
                self._inbox.put_nowait(_Submit(leftover))

    def _broadcast(self, event: LoopEvent) -> None:
        self._seq += 1
        envelope = ConversationEvent(
            conversation_id=self._conversation_id,
            seq=self._seq,
            payload=event,
        )
        for queue in self._subscribers:
            with suppress(asyncio.QueueFull):  # slow subscribers drop events
                queue.put_nowait(envelope)

    @staticmethod
    def _format_task_done(task: Task) -> str:
        result = task.result if task.result is not None else f"error: {task.error}"
        return TASK_DONE_TEMPLATE.format(title=task.title, status=task.status.value, result=result)


class ConversationManager:
    """Owns one runner per conversation (in-memory)."""

    def __init__(self, loop: AgentLoop, system_prompt: str) -> None:
        self._loop = loop
        self._system_prompt = system_prompt
        self._runners: dict[str, ConversationRunner] = {}

    def create_conversation(self) -> str:
        """Create and start a runner for a new conversation."""
        conversation_id = uuid.uuid4().hex
        runner = ConversationRunner(conversation_id, self._loop, self._system_prompt)
        runner.start()
        self._runners[conversation_id] = runner
        return conversation_id

    def get(self, conversation_id: str) -> ConversationRunner:
        """Return the runner or raise ConversationNotFoundError."""
        try:
            return self._runners[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError(conversation_id) from exc

    async def notify_task_done(self, task: Task) -> None:
        """Deliver a finished task to its conversation, if still around."""
        runner = self._runners.get(task.conversation_id)
        if runner is not None:
            await runner.notify_task_done(task)
