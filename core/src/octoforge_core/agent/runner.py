"""Per-dialog actor: serializes commands, persists history and broadcasts loop events."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import Failed, LoopEvent
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.ports import TaskStore
from octoforge_core.skills.base import SkillContext
from octoforge_core.tasks.models import Task

SUBSCRIBER_QUEUE_SIZE = 100
TASK_DONE_TEMPLATE = (
    "Background task '{title}' has finished with status {status}.\nResult:\n{result}"
)


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A loop event wrapped with dialog metadata."""

    dialog_id: str
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
    """Actor owning a dialog's history, loop runs and subscribers."""

    def __init__(
        self,
        dialog: Dialog,
        loop: AgentLoop,
        system_prompt: str,
        messages: MessageRepository,
        history: list[ChatMessage],
    ) -> None:
        self._dialog = dialog
        self._loop = loop
        self._system_prompt = system_prompt
        self._messages = messages
        self._history = history
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
        """Deliver a finished background task to the dialog."""
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
        """Return a copy of the dialog history."""
        return list(self._history)

    @property
    def dialog_id(self) -> str:
        """Return the id of the owned dialog."""
        return self._dialog.id

    async def _run_actor(self) -> None:
        while True:
            command = await self._inbox.get()
            if isinstance(command, _Submit):
                await self._handle_submit(command.message)
            elif isinstance(command, _Cancel):
                self._handle_cancel()
            elif isinstance(command, _TaskDone):
                await self._handle_task_done(command.task)

    def _is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    async def _handle_submit(self, message: ChatMessage) -> None:
        if self._is_running() and self._control is not None:
            self._control.inject(message)
            return
        await self._persist(message)
        self._history.append(message)
        self._start_run()

    def _handle_cancel(self) -> None:
        if self._control is not None:
            self._control.cancel()

    async def _handle_task_done(self, task: Task) -> None:
        notification = ChatMessage(role=MessageRole.SYSTEM, content=self._format_task_done(task))
        if self._is_running() and self._control is not None:
            self._control.inject(notification)
            return
        await self._persist(notification)
        self._history.append(notification)
        self._start_run()

    def _start_run(self) -> None:
        control = LoopControl()
        self._control = control
        self._run_task = asyncio.create_task(self._pump(control))

    async def _pump(self, control: LoopControl) -> None:
        context = SkillContext(
            user_id=self._dialog.user_id,
            channel=self._dialog.channel,
            dialog_id=self._dialog.id,
        )
        old_size = len(self._history)
        working = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._system_prompt),
            *self._history,
        ]
        persisted = len(working)
        try:
            async for event in self._loop.stream(working, control, context):
                self._broadcast(event)
                persisted = await self._persist_new(working, persisted)
        except Exception as exc:  # loop failures are broadcast, not raised
            self._broadcast(Failed(error=str(exc)))
        finally:
            await self._persist_new(working, persisted)
            self._history.extend(working[old_size + 1 :])
            self._control = None
            for leftover in control.drain():
                self._inbox.put_nowait(_Submit(leftover))

    async def _persist(self, message: ChatMessage) -> None:
        await self._messages.append(self._dialog.id, message)

    async def _persist_new(self, working: list[ChatMessage], persisted: int) -> int:
        """Store messages appended to the working history since the last flush."""
        for message in working[persisted:]:
            await self._persist(message)
        return len(working)

    def _broadcast(self, event: LoopEvent) -> None:
        self._seq += 1
        envelope = ConversationEvent(
            dialog_id=self._dialog.id,
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
    """Owns one runner per dialog, keyed by (user_id, channel)."""

    def __init__(
        self,
        loop: AgentLoop,
        system_prompt: str,
        dialogs: DialogRepository,
        messages: MessageRepository,
        tasks: TaskStore,
    ) -> None:
        self._loop = loop
        self._system_prompt = system_prompt
        self._dialogs = dialogs
        self._messages = messages
        self._tasks = tasks
        self._runners: dict[str, ConversationRunner] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_runner(self, user_id: str, channel: str) -> ConversationRunner:
        """Return the live runner for (user_id, channel); the dialog is created on first contact.

        The runner history is rebuilt from the persisted messages, so a dialog
        survives process restarts.
        """
        async with self._lock:
            dialog = await self._dialogs.get_or_create(user_id, channel)
            runner = self._runners.get(dialog.id)
            if runner is None:
                history = await self._messages.list(dialog.id)
                runner = ConversationRunner(
                    dialog=dialog,
                    loop=self._loop,
                    system_prompt=self._system_prompt,
                    messages=self._messages,
                    history=history,
                )
                runner.start()
                self._runners[dialog.id] = runner
            return runner

    async def notify_task_done(self, task: Task) -> None:
        """Deliver a finished task to its dialog and mark the result delivered."""
        runner = self._runners.get(task.dialog_id)
        if runner is None:
            return
        await runner.notify_task_done(task)
        await self._tasks.mark_delivered(task.id)
