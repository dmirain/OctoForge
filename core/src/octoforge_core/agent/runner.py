"""Per-dialog actor: owns the narrative and the processes answering user questions."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessCompleted,
    ProcessResumed,
    ProcessSuspended,
)
from octoforge_core.agent.loop import AgentLoop, format_error
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, PromptProvider
from octoforge_core.agent.router import (
    MessageRouter,
    ProcessInfo,
    ProcessPlace,
    RouteAction,
    RouteDecision,
    RouteOp,
)
from octoforge_core.context.api import ContextCompactor
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.llm.usage import Usage
from octoforge_core.ports import TaskStore
from octoforge_core.skills.base import SkillContext
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.spawner import TaskSpawner
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 100
TITLE_MAX_LENGTH = 60
REPORT_TITLE = "report"
REPORT_NUDGE = (
    "The system notification above is new: briefly report it to the user "
    "in the user's language, then stop."
)
INTERRUPTED_NOTE = "[The previous assistant message was interrupted and may be incomplete.]"
TASK_DONE_TEMPLATE = (
    "Background task '{title}' has finished with status {status}.\nResult:\n{result}"
)
LIMIT_REFUSAL_TEMPLATE = (
    "The user asked: '{message}' — but the process limit ({limit}) is reached and the "
    "request was not started. Propose cancelling one of the active processes: {titles}."
)
SPAWN_REFUSAL_TEMPLATE = (
    "cannot spawn: process limit ({limit}) reached; active: {titles} — ask the user what to cancel"
)
CRON_LIMIT_NOTE_TEMPLATE = (
    "Cron job '{title}' could not start: process limit ({limit}) reached; active: {titles}"
)
SPAWNED_TEMPLATE = "task {task_id} spawned"
BACKGROUND_TASK_PROMPT = (
    "You are solving a background task. User message is the task. "
    "Produce the final answer as the result."
)
DATE_ENVELOPE_TEMPLATE = "[Current date and time: {now} (UTC)]\n{content}"
CURRENT_DATE_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A loop event wrapped with dialog metadata."""

    dialog_id: str
    seq: int
    payload: LoopEvent


def _with_date_envelope(message: ChatMessage) -> ChatMessage:
    """Stamp the current UTC date/time on a copy of the branch's last message.

    The volatile timestamp rides the tail of the prompt (the narrative and the
    store keep the clean copy), so the system prompt — the long cacheable
    prefix — stays byte-stable across runs and processes.
    """
    return ChatMessage(
        role=message.role,
        content=DATE_ENVELOPE_TEMPLATE.format(
            now=utc_now().strftime(CURRENT_DATE_FORMAT), content=message.content
        ),
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
    )


@dataclass(frozen=True, slots=True)
class _Submit:
    """A message to route; recorded ones already live in the narrative."""

    message: ChatMessage
    recorded: bool = False
    client_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Cancel:
    pass


@dataclass(frozen=True, slots=True)
class _ProcessTerminated:
    process_id: str
    task_id: str | None


_Command = _Submit | _Cancel | _ProcessTerminated


@dataclass(slots=True)
class _Process:
    """One question being processed: its own loop run and history branch."""

    id: str
    title: str
    task_id: str | None
    control: LoopControl
    branch: list[ChatMessage]
    pump: asyncio.Task[None] | None = None
    # Branch layout for the reactive-compaction rebuild: the head (system
    # prompt) and the trail survive, the narrative between them is re-assembled
    # after compaction. head_len=0 marks branches not built from the narrative
    # (background tasks) — they are not rebuilt.
    head_len: int = 0
    trail: ChatMessage | None = None
    overflow_retried: bool = False


class TaskOutcomeListener(Protocol):
    """Port reporting the terminal status of a task-backed process (e.g. cron)."""

    async def report_outcome(self, task: Task, status: TaskStatus) -> None:
        """React to a finished task; reporting must never break the dialog."""
        ...


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Behavior parameters shared by the conversation runners of one manager."""

    loop: AgentLoop
    prompts: PromptProvider
    router: MessageRouter
    max_processes: int
    compactor: ContextCompactor
    task_outcome_listener: TaskOutcomeListener | None = None


class ConversationRunner:
    """Actor owning a dialog's narrative, processes and subscribers."""

    def __init__(
        self,
        dialog: Dialog,
        config: RunnerConfig,
        messages: MessageRepository,
        tasks: TaskStore,
        history: list[ChatMessage],
    ) -> None:
        self._dialog = dialog
        self._loop = config.loop
        self._prompts = config.prompts
        self._router = config.router
        self._max_processes = config.max_processes
        self._task_outcome_listener = config.task_outcome_listener
        self._compactor = config.compactor
        self._messages = messages
        self._tasks = tasks
        self._narrative = history
        self._processes: dict[str, _Process] = {}
        self._foreground_id: str | None = None
        self._spawner: TaskSpawner = _DialogTaskSpawner(self)
        self._inbox: asyncio.Queue[_Command] = asyncio.Queue()
        self._subscribers: set[asyncio.Queue[ConversationEvent]] = set()
        self._seq = 0
        self._dropped_events = 0
        self._actor_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start consuming the inbox."""
        if self._actor_task is None:
            self._actor_task = asyncio.create_task(self._run_actor())
            self._actor_task.add_done_callback(self._on_actor_done)

    def _on_actor_done(self, task: asyncio.Task[None]) -> None:
        """Surface an unexpected actor exit; a cancelled stop() is expected."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("actor task exited unexpectedly: dialog=%s", self._dialog.id, exc_info=exc)

    async def stop(self) -> None:
        """Cancel all processes and the actor itself."""
        for process in self._processes.values():
            process.control.cancel()
        if self._actor_task is not None:
            self._actor_task.cancel()
        await self._compactor.aclose()

    async def submit(self, content: str, client_message_id: str | None = None) -> None:
        """Submit a user message; the router decides how it maps to processes.

        `client_message_id` is an idempotency key: a repeat with an
        already-recorded key is skipped (delivery retries are normal).
        """
        await self._inbox.put(
            _Submit(
                ChatMessage(role=MessageRole.USER, content=content),
                client_message_id=client_message_id,
            )
        )

    async def cancel(self) -> None:
        """Cancel the foreground process, if any (explicit user request)."""
        await self._inbox.put(_Cancel())

    async def spawn_task(self, title: str, prompt: str) -> str:
        """Create a RUN task with its background process; refuse when the limit is hit."""
        if len(self._processes) >= self._max_processes:
            return SPAWN_REFUSAL_TEMPLATE.format(
                limit=self._max_processes, titles=self._active_titles()
            )
        task = await self._spawn_process_task(title, prompt, cron_job_id=None)
        return SPAWNED_TEMPLATE.format(task_id=task.id)

    async def wake(self, title: str, prompt: str, cron_job_id: str) -> None:
        """Start a cron-fired background process tagged with its cron job id.

        Unlike `spawn_task`, hitting the process limit publishes a system note
        (the delayed impossibility notification) instead of returning a text.
        """
        if len(self._processes) >= self._max_processes:
            note = CRON_LIMIT_NOTE_TEMPLATE.format(
                title=title, limit=self._max_processes, titles=self._active_titles()
            )
            await self._publish_system_note(note)
            return
        await self._spawn_process_task(title, prompt, cron_job_id=cron_job_id)

    async def _spawn_process_task(
        self,
        title: str,
        prompt: str,
        cron_job_id: str | None,
    ) -> Task:
        """Create a RUN task with its background process; the limit check is the caller's."""
        task_input: dict[str, Any] = {"title": title, "prompt": prompt}
        if cron_job_id is not None:
            task_input["cron_job_id"] = cron_job_id
        task = Task(
            dialog_id=self._dialog.id,
            user_id=self._dialog.user_id,
            channel=self._dialog.channel,
            title=title,
            kind=TaskKind.RUN,
            input=task_input,
        )
        await self._tasks.add(task)
        await self._tasks.mark_running(task)
        self._create_process(
            process_id=task.id,
            title=title,
            task_id=task.id,
            branch=[
                ChatMessage(role=MessageRole.SYSTEM, content=BACKGROUND_TASK_PROMPT),
                _with_date_envelope(ChatMessage(role=MessageRole.USER, content=prompt)),
            ],
        )
        return task

    def subscribe(self) -> asyncio.Queue[ConversationEvent]:
        """Attach a subscriber queue receiving broadcast events."""
        queue: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ConversationEvent]) -> None:
        """Detach a subscriber queue."""
        self._subscribers.discard(queue)

    def history(self) -> list[ChatMessage]:
        """Return a copy of the dialog narrative."""
        return list(self._narrative)

    @property
    def dialog_id(self) -> str:
        """Return the id of the owned dialog."""
        return self._dialog.id

    async def _run_actor(self) -> None:
        while True:
            command = await self._inbox.get()
            try:
                await self._dispatch(command)
            except Exception:  # one bad command must not zombie the whole dialog
                logger.exception(
                    "actor command failed: dialog=%s command=%s",
                    self._dialog.id,
                    type(command).__name__,
                )

    async def _dispatch(self, command: _Command) -> None:
        if isinstance(command, _Submit):
            await self._handle_submit(command)
        elif isinstance(command, _Cancel):
            self._handle_cancel()
        elif isinstance(command, _ProcessTerminated):
            await self._handle_terminated(command)

    async def _handle_submit(self, command: _Submit) -> None:
        message = command.message
        if not command.recorded:
            if await self._is_duplicate(command.client_message_id):
                logger.info(
                    "duplicate submit skipped: dialog=%s key=%s",
                    self._dialog.id,
                    command.client_message_id,
                )
                return
            await self._persist(message, client_message_id=command.client_message_id)
            self._narrative.append(message)
        decision = await self._router.route(self._snapshot(), message.content, self._max_processes)
        await self._apply_decision(message, decision)

    async def _is_duplicate(self, client_message_id: str | None) -> bool:
        """Whether a submit with this idempotency key was already recorded."""
        if client_message_id is None:
            return False
        return await self._messages.find_by_client_id(self._dialog.id, client_message_id)

    def _handle_cancel(self) -> None:
        foreground = self._foreground()
        if foreground is not None:
            foreground.control.cancel()

    async def _handle_terminated(self, command: _ProcessTerminated) -> None:
        """Deliver a finished task result to the dialog exactly once."""
        if command.task_id is None:
            return
        task = await self._tasks.get(command.task_id)
        if task.status not in (TaskStatus.DONE, TaskStatus.FAILED) or task.result_delivered:
            return
        await self._tasks.mark_delivered(task.id)
        await self._publish_system_note(self._format_task_done(task))

    def _snapshot(self) -> tuple[ProcessInfo, ...]:
        return tuple(
            ProcessInfo(
                id=process.id,
                title=process.title,
                place=(
                    ProcessPlace.FOREGROUND
                    if process.id == self._foreground_id
                    else ProcessPlace.BACKGROUND
                ),
            )
            for process in self._processes.values()
        )

    async def _apply_decision(self, message: ChatMessage, decision: RouteDecision) -> None:
        ops = decision.ops or (RouteOp(action=RouteAction.START_NEW),)
        cancelled: set[str] = set()
        for op in ops:
            if op.action is RouteAction.CANCEL:
                if op.target_id is not None and self._cancel_process(op.target_id):
                    cancelled.add(op.target_id)
            elif op.action is RouteAction.INJECT:
                await self._apply_inject(message, cancelled)
            elif op.action is RouteAction.START_NEW:
                await self._apply_start_new(message, cancelled)
            elif op.action is RouteAction.PROMOTE:
                await self._apply_promote(message, op.target_id, cancelled)

    async def _apply_inject(
        self,
        message: ChatMessage,
        cancelled: set[str],
    ) -> None:
        foreground = self._foreground()
        if foreground is not None:
            foreground.control.inject(message)
            return
        await self._apply_start_new(message, cancelled)

    async def _apply_start_new(
        self,
        message: ChatMessage,
        cancelled: set[str],
    ) -> None:
        if self._exceeds_limit(cancelled):
            await self._reject_for_limit(message)
            return
        await self._start_new(title=message.content[:TITLE_MAX_LENGTH])

    async def _apply_promote(
        self,
        message: ChatMessage,
        target_id: str | None,
        cancelled: set[str],
    ) -> None:
        target = self._processes.get(target_id) if target_id is not None else None
        if target is None or target.id == self._foreground_id:
            return  # only an active background process can be promoted
        if self._exceeds_limit(cancelled):
            await self._reject_for_limit(message)
            return
        self._suspend_foreground()
        self._foreground_id = target.id
        self._broadcast(ProcessResumed(process_id=target.id, title=target.title))

    def _exceeds_limit(self, cancelled: set[str]) -> bool:
        return len(self._processes) - len(cancelled) + 1 > self._max_processes

    async def _reject_for_limit(self, message: ChatMessage) -> None:
        note = LIMIT_REFUSAL_TEMPLATE.format(
            message=message.content,
            limit=self._max_processes,
            titles=self._active_titles(),
        )
        await self._publish_system_note(note)

    async def _start_new(
        self,
        title: str,
        trail: ChatMessage | None = None,
    ) -> None:
        """Start a foreground process over the compacted narrative, suspending the old one."""
        self._suspend_foreground()
        narrative = await self._compactor.assemble(self._dialog, self._narrative)
        branch = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._prompts.get(SYSTEM_PROMPT_NAME)),
            *narrative,
            *([trail] if trail is not None else []),
        ]
        if len(branch) > 1:
            branch[-1] = _with_date_envelope(branch[-1])
        process = self._create_process(
            process_id=uuid.uuid4().hex,
            title=title,
            task_id=None,
            branch=branch,
        )
        process.head_len = 1
        process.trail = trail
        self._foreground_id = process.id

    async def _start_report_run(self) -> None:
        """Start a foreground run reacting to the latest narrative note (fg is free).

        A trailing system note leaves some models without anything to answer
        to (they reason but emit no content), so the run ends with a user-role
        nudge asking for the report.
        """
        await self._start_new(
            title=REPORT_TITLE,
            trail=ChatMessage(role=MessageRole.USER, content=REPORT_NUDGE),
        )

    def _suspend_foreground(self) -> None:
        foreground = self._foreground()
        if foreground is None:
            return
        self._foreground_id = None
        self._broadcast(ProcessSuspended(process_id=foreground.id, title=foreground.title))

    def _cancel_process(self, target_id: str) -> bool:
        process = self._processes.get(target_id)
        if process is None:
            return False
        process.control.cancel()
        return True

    def _create_process(
        self,
        *,
        process_id: str,
        title: str,
        task_id: str | None,
        branch: list[ChatMessage],
    ) -> _Process:
        process = _Process(
            id=process_id,
            title=title,
            task_id=task_id,
            control=LoopControl(),
            branch=branch,
        )
        process.pump = asyncio.create_task(self._pump_process(process))
        self._processes[process.id] = process
        return process

    async def _pump_process(self, process: _Process) -> None:
        """Stream the process loop, then always finalize and release the slot.

        Finalization writes to the store; even if that fails, the process is
        removed and its termination is signalled so the slot is never leaked.
        """
        terminal = await self._stream_terminal(process)
        status = TaskStatus.FAILED
        try:
            status = await self._finalize(process, terminal)
        except Exception:  # a store failure must not wedge the process slot
            logger.exception(
                "process finalize failed: dialog=%s process=%s", self._dialog.id, process.id
            )
        finally:
            self._terminate_process(process, status)

    async def _stream_terminal(self, process: _Process) -> LoopEvent:
        """Run the loop stream, compacting reactively once on a context overflow.

        An overflow fails the run only when the process was already retried or
        its branch is not narrative-built (background tasks): a retry with the
        same oversized branch would just overflow again.
        """
        while True:
            try:
                return await self._stream_once(process)
            except ContextOverflowError as exc:
                if process.overflow_retried or process.head_len == 0:
                    return self._fail_run(process, format_error(exc))
                process.overflow_retried = True
                logger.info(
                    "context overflow, compacting reactively: dialog=%s process=%s",
                    self._dialog.id,
                    process.id,
                )
                if not await self._compactor.compact_now(self._dialog):
                    return self._fail_run(process, format_error(exc))
                await self._rebuild_branch(process)

    async def _rebuild_branch(self, process: _Process) -> None:
        """Re-assemble the narrative part of the branch after a compaction."""
        narrative = await self._compactor.assemble(self._dialog, self._narrative)
        process.branch[:] = [
            *process.branch[: process.head_len],
            *narrative,
            *([process.trail] if process.trail is not None else []),
        ]
        if len(process.branch) > process.head_len:
            process.branch[-1] = _with_date_envelope(process.branch[-1])

    def _fail_run(self, process: _Process, error: str) -> LoopEvent:
        """Broadcast and return a Failed terminal for the process."""
        terminal = Failed(error=error)
        if self._foreground_id == process.id:
            self._broadcast(terminal)
        return terminal

    async def _stream_once(self, process: _Process) -> LoopEvent:
        """Run the loop stream, broadcasting events only while it is the foreground."""
        context = SkillContext(
            user_id=self._dialog.user_id,
            channel=self._dialog.channel,
            dialog_id=self._dialog.id,
            task_spawner=self._spawner,
        )
        terminal: LoopEvent = Failed(error="loop ended without a terminal event")
        try:
            async for event in self._loop.stream(process.branch, process.control, context):
                if self._foreground_id == process.id:
                    self._broadcast(event)
                if isinstance(event, (Finished, Cancelled, Failed)):
                    terminal = event
        except ContextOverflowError:
            raise  # the reactive-compaction retry handles it one level up
        except Exception as exc:  # loop failures are broadcast, not raised
            logger.exception(
                "process loop crashed: dialog=%s process=%s", self._dialog.id, process.id
            )
            terminal = Failed(error=format_error(exc))
            if self._foreground_id == process.id:
                self._broadcast(terminal)
        return terminal

    def _terminate_process(self, process: _Process, status: TaskStatus) -> None:
        """Remove the process, announce completion and requeue any drained injections."""
        self._remove_process(process)
        self._broadcast(
            ProcessCompleted(process_id=process.id, title=process.title, status=status.value)
        )
        self._inbox.put_nowait(_ProcessTerminated(process_id=process.id, task_id=process.task_id))
        for leftover in process.control.drain():
            self._inbox.put_nowait(_Submit(leftover, recorded=True))

    async def _finalize(self, process: _Process, terminal: LoopEvent) -> TaskStatus:
        """Fold the run outcome into the narrative and the task store."""
        if isinstance(terminal, Finished):
            await self._persist(terminal.message, usage=terminal.usage)
            self._narrative.append(terminal.message)
            await self._resolve_task(process, result=terminal.message.content)
            status = TaskStatus.DONE
        elif isinstance(terminal, Failed):
            await self._fail_task(process, terminal.error)
            status = TaskStatus.FAILED
        else:
            await self._cancel_task(process)
            await self._salvage_interrupted_turn(process)
            status = TaskStatus.CANCELLED
        await self._report_outcome(process, status)
        return status

    async def _report_outcome(self, process: _Process, status: TaskStatus) -> None:
        """Tell the outcome listener about a finished cron-tagged task, if any."""
        listener = self._task_outcome_listener
        if listener is None or process.task_id is None:
            return
        task = await self._tasks.get(process.task_id)
        if "cron_job_id" not in task.input:
            return
        try:
            await listener.report_outcome(task, status)
        except Exception:  # outcome reporting must not break the dialog
            logger.exception(
                "task outcome report failed: dialog=%s task=%s", self._dialog.id, task.id
            )

    async def _resolve_task(self, process: _Process, result: str) -> None:
        if process.task_id is None:
            return
        task = await self._tasks.get(process.task_id)
        await self._tasks.mark_done(task, result)

    async def _fail_task(self, process: _Process, error: str) -> None:
        if process.task_id is None:
            return
        task = await self._tasks.get(process.task_id)
        await self._tasks.mark_failed(task, error)

    async def _cancel_task(self, process: _Process) -> None:
        if process.task_id is not None:
            await self._tasks.cancel(process.task_id)

    async def _salvage_interrupted_turn(self, process: _Process) -> None:
        """Keep a cancelled run's partial answer in the narrative, flagged as incomplete."""
        last = process.branch[-1] if process.branch else None
        if last is None or last.role is not MessageRole.ASSISTANT or not last.content:
            return
        note = ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE)
        await self._persist(last)
        await self._persist(note)
        self._narrative.extend((last, note))

    def _remove_process(self, process: _Process) -> None:
        self._processes.pop(process.id, None)
        if self._foreground_id == process.id:
            self._foreground_id = None

    def _foreground(self) -> _Process | None:
        if self._foreground_id is None:
            return None
        return self._processes.get(self._foreground_id)

    def _active_titles(self) -> str:
        return ", ".join(process.title for process in self._processes.values())

    async def _publish_system_note(self, content: str) -> None:
        """Record a system note in the narrative and route it: inject or report run."""
        note = ChatMessage(role=MessageRole.SYSTEM, content=content)
        await self._persist(note)
        self._narrative.append(note)
        foreground = self._foreground()
        if foreground is not None:
            foreground.control.inject(note)
        else:
            await self._start_report_run()

    async def _persist(
        self,
        message: ChatMessage,
        usage: Usage | None = None,
        client_message_id: str | None = None,
    ) -> None:
        await self._messages.append(
            self._dialog.id, message, usage=usage, client_message_id=client_message_id
        )

    def _broadcast(self, event: LoopEvent) -> None:
        self._seq += 1
        envelope = ConversationEvent(
            dialog_id=self._dialog.id,
            seq=self._seq,
            payload=event,
        )
        for queue in self._subscribers:
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:  # slow subscribers drop events; keep it countable
                self._dropped_events += 1
                logger.debug(
                    "dropped SSE event: dialog=%s seq=%s dropped_total=%s",
                    self._dialog.id,
                    self._seq,
                    self._dropped_events,
                )

    @staticmethod
    def _format_task_done(task: Task) -> str:
        result = task.result if task.result is not None else f"error: {task.error}"
        return TASK_DONE_TEMPLATE.format(title=task.title, status=task.status.value, result=result)


class _DialogTaskSpawner:
    """TaskSpawner port bound to one dialog: tasks become actor background processes."""

    def __init__(self, runner: ConversationRunner) -> None:
        self._runner = runner

    async def spawn(self, title: str, prompt: str) -> str:
        return await self._runner.spawn_task(title, prompt)


class ConversationManager:
    """Owns one runner per dialog, keyed by (user_id, channel)."""

    def __init__(
        self,
        config: RunnerConfig,
        dialogs: DialogRepository,
        messages: MessageRepository,
        tasks: TaskStore,
    ) -> None:
        self._config = config
        self._dialogs = dialogs
        self._messages = messages
        self._tasks = tasks
        self._runners: dict[str, ConversationRunner] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_runner(self, user_id: str, channel: str) -> ConversationRunner:
        """Return the live runner for (user_id, channel); the dialog is created on first contact.

        The runner narrative is rebuilt from the persisted messages, so a dialog
        survives process restarts (in-flight processes do not).
        """
        async with self._lock:
            dialog = await self._dialogs.get_or_create(user_id, channel)
            runner = self._runners.get(dialog.id)
            if runner is None:
                history = await self._messages.list(dialog.id)
                runner = ConversationRunner(
                    dialog=dialog,
                    config=self._config,
                    messages=self._messages,
                    tasks=self._tasks,
                    history=history,
                )
                runner.start()
                self._runners[dialog.id] = runner
            return runner

    async def wake(
        self,
        user_id: str,
        channel: str,
        title: str,
        prompt: str,
        cron_job_id: str,
    ) -> None:
        """Deliver a cron firing into the user's dialog as a background process."""
        runner = await self.get_or_create_runner(user_id, channel)
        await runner.wake(title, prompt, cron_job_id)
