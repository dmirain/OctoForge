"""Per-dialog actor: owns the narrative, the task processes and result delivery.

The actor is a message broker. Every process is backed by a task row
(ANSWER for user questions, RUN for deferred/cron work). A process branch
is `[system] + narrative snapshot + private working suffix`: instead of an
inject channel, the branch re-syncs its narrative part from the actor's
narrative at every iteration boundary (the pull model), so a message lands
in the narrative exactly once and every running process sees it. Finished
tasks are delivered through the outbox (`_pending_deliveries`): foreground
(streamed) tasks are only stamped delivered, background ones are broadcast
as a whole (TextDelta + Finished / Failed) once the foreground is free and
a transport is attached (with no subscriber the outbox waits — see
`_flush_deliveries`). There is no report run — delivery never involves an
LLM call.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Protocol

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    ProcessCompleted,
    ProcessSuspended,
    TextDelta,
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
from octoforge_core.context.api import INTERRUPTED_NOTE, ContextCompactor
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.llm.usage import Usage
from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.spawner import TaskDeleteOutcome, TaskDeleter, TaskSpawner
from octoforge_core.tasks.store import TaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 100
# events a transport must never miss: terminals close a streamed message and
# gate `delivered_at`, process markers drive the surface's UI state. Stream
# chatter (TextDelta, tool events) may drop on a lagging subscriber instead.
_CRITICAL_EVENTS = (Finished, Failed, Cancelled, ProcessSuspended, ProcessCompleted)
TITLE_MAX_LENGTH = 60
SPAWN_REFUSAL_TEMPLATE = (
    "cannot spawn: process limit ({limit}) reached; active: {titles} — ask the user what to cancel"
)
PROCESS_LIMIT_NOTICE_TEMPLATE = (
    "I could not start handling '{message}': the process limit ({limit}) is reached. "
    "Active processes: {titles}. Cancel one of them or wait for it to finish."
)
CRON_LIMIT_NOTICE_TEMPLATE = (
    "Cron job '{title}' could not start: the process limit ({limit}) is reached. "
    "Active processes: {titles}."
)
SPAWNED_TEMPLATE = "task {task_id} spawned"
RESTART_LIMIT_ERROR = "could not resume after the service restart: process limit reached"
DEFAULT_TASK_ERROR = "unknown error"
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


def _latest_assistant_with_content(branch: list[ChatMessage]) -> ChatMessage | None:
    """Return the newest assistant message with content, skipping a trailing tool tail.

    An interrupted tool iteration appends its tool replies after the assistant
    message, so the salvaged turn is not necessarily the branch tail. Tool
    messages are never persisted to the narrative, so the history stays valid.
    """
    for message in reversed(branch):
        if message.role is MessageRole.TOOL:
            continue
        if message.role is MessageRole.ASSISTANT and message.content:
            return message
        return None
    return None


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
class _Flush:
    """A fresh subscriber asking the actor to drain the delivery outbox."""


@dataclass(frozen=True, slots=True)
class _ProcessTerminated:
    """A process (or a recovery sweep) asking the actor to deliver a task outcome.

    `terminal` is the live run's Finished/Failed event (None for cancellations
    and recovery redeliveries — the stored task status decides then);
    `streamed` tells whether the process was the foreground at termination,
    i.e. the user already watched its outcome live.
    """

    task_id: str
    terminal: Finished | Failed | None = None
    streamed: bool = False


@dataclass(frozen=True, slots=True)
class _Delivery:
    """Events of one finished task awaiting transport delivery."""

    events: tuple[LoopEvent, ...]
    task_id: str | None


_Command = _Submit | _ProcessTerminated | _Flush


@dataclass(slots=True)
class _Process:
    """One task being processed: its own loop run and history branch.

    Pull-model bookkeeping (narrative-built branches only): `synced_len` is
    the branch length right after the last narrative sync — everything past
    it is the private working suffix (assistant/tool messages of the run)
    that survives every re-sync; `watermark` is the narrative length at that
    sync. `narrative_built=False` marks self-contained branches (RUN tasks)
    that never sync.
    """

    id: str
    title: str
    task_id: str
    control: LoopControl
    branch: list[ChatMessage]
    pump: asyncio.Task[None] | None = None
    narrative_built: bool = False
    synced_len: int = 0
    watermark: int = 0
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
    """Actor owning a dialog's narrative, processes, deliveries and subscribers."""

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
        self._pending_deliveries: list[_Delivery] = []
        # row ids of user messages that need no (further) answer: an answer
        # task was created from them, or they were pure commands. Ids, not
        # narrative indices — the narrative is trimmed to the hot tail as
        # compaction advances, and indices would shift under a queued command
        self._covered_ids: set[str] = set()
        # serializes the limit-check → process-create sequence between the
        # actor (`_apply_start_new`) and direct callers (`spawn_task`/`wake`),
        # which run in pump/scheduler tasks outside the actor's inbox
        self._spawn_lock = asyncio.Lock()
        # serializes outbox flushes between the actor and wake()/restart_task()
        self._flush_lock = asyncio.Lock()
        self._spawner: TaskSpawner = _DialogTaskSpawner(self)
        self._deleter: TaskDeleter = _DialogTaskDeleter(self)
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
        """Cancel all processes and the actor itself, awaiting their shutdown.

        The pump tasks are cancelled directly (a hung LLM stream must not
        stall the shutdown) and awaited, so no runner task outlives stop();
        only this dialog's background compaction is cancelled (the compactor
        instance is shared with the other runners).
        """
        for process in self._processes.values():
            process.control.cancel()
        pending: list[asyncio.Task[None]] = []
        for process in self._processes.values():
            if process.pump is not None:
                process.pump.cancel()
                pending.append(process.pump)
        if self._actor_task is not None:
            self._actor_task.cancel()
            pending.append(self._actor_task)
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        await self._compactor.aclose(self._dialog.id)

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
        """Cancel the foreground process, if any (explicit user request).

        Applied directly, not through the inbox: the actor may be busy inside
        a routing LLM call (up to OF_ROUTER_TIMEOUT_SECONDS), and a queued
        cancel would wait behind it — a stop must act now. Only the running
        LoopControl and the foreground pointer are touched, both owned by
        this event loop, so bypassing the inbox is race-free.
        """
        self._handle_cancel()

    async def spawn_task(self, title: str, prompt: str) -> str:
        """Create a RUN task with its background process; refuse when the limit is hit."""
        async with self._spawn_lock:
            if len(self._processes) >= self._max_processes:
                return self._spawn_refusal()
            task = await self._prepare_process_task(
                title, prompt, kind=TaskKind.RUN, cron_job_id=None
            )
            self._start_process(task)
        return SPAWNED_TEMPLATE.format(task_id=task.id)

    async def wake(self, title: str, prompt: str, cron_job_id: str) -> bool:
        """Start a cron-fired background process tagged with its cron job id.

        Unlike `spawn_task`, hitting the process limit publishes a canned
        broker notice (the delayed impossibility notification) instead of
        returning a text. Returns whether the process was actually started,
        so the caller (the scheduler) can tell a real delivery from a
        limit-skip and avoid advancing the job's schedule on a skip.
        """
        async with self._spawn_lock:
            over_limit = len(self._processes) >= self._max_processes
            if not over_limit:
                task = await self._prepare_process_task(
                    title, prompt, kind=TaskKind.RUN, cron_job_id=cron_job_id
                )
                self._start_process(task)
        if over_limit:
            await self._publish_cron_limit_note(title)
        return not over_limit

    async def delete_task(self, task_id: str) -> TaskDeleteOutcome:
        """Stop a live task process so it reaches a terminal state.

        The finalization that follows the stop marks the row CANCELLED (task
        rows are kept forever). The caller must not pass the id of the
        process it runs in (the pump cannot be awaited from within) —
        `TaskDeleteTool` refuses that via `ToolContext.owner_task_id`.
        """
        process = self._processes.get(task_id)
        if process is None:
            return TaskDeleteOutcome.NOT_RUNNING
        process.control.cancel()
        if process.pump is not None:
            await process.pump
        return TaskDeleteOutcome.DELETED

    async def restart_task(self, task: Task) -> None:
        """Restart a task orphaned by a service restart (always queue-mode).

        RUN branches are self-contained (system prompt + task prompt);
        ANSWER branches re-attach to the narrative (the question lives
        there). Over the process limit the task is failed instead and the
        failure is queued for delivery.
        """
        async with self._spawn_lock:
            if len(self._processes) < self._max_processes:
                await self._start_orphaned(task)
                return
        await self._tasks.mark_failed(task, RESTART_LIMIT_ERROR)
        self._pending_deliveries.append(
            _Delivery(events=(Failed(error=RESTART_LIMIT_ERROR),), task_id=task.id)
        )
        await self._flush_if_free()

    async def _start_orphaned(self, task: Task) -> None:
        """Start the replacement background process of an orphaned task."""
        if task.kind is TaskKind.ANSWER:
            narrative = await self._assemble_narrative()
            process = self._create_process(
                task=task,
                branch=[self._system_message(), *narrative],
                narrative_built=True,
            )
            process.synced_len = len(process.branch)
            process.watermark = len(self._narrative)
        else:
            self._start_process(task)

    def _spawn_refusal(self) -> str:
        return SPAWN_REFUSAL_TEMPLATE.format(
            limit=self._max_processes, titles=self._active_titles()
        )

    async def _publish_cron_limit_note(self, title: str) -> None:
        notice = CRON_LIMIT_NOTICE_TEMPLATE.format(
            title=title, limit=self._max_processes, titles=self._active_titles()
        )
        await self._deliver_notice(notice)

    async def _deliver_notice(self, content: str) -> None:
        """Persist a canned broker notice as an assistant message and queue it."""
        notice = ChatMessage(role=MessageRole.ASSISTANT, content=content)
        await self._persist(notice)
        self._narrative.append(notice)
        self._pending_deliveries.append(
            _Delivery(
                events=(TextDelta(text=content), Finished(message=notice)),
                task_id=None,
            )
        )
        await self._flush_if_free()

    async def _prepare_process_task(
        self,
        title: str,
        prompt: str,
        *,
        kind: TaskKind,
        cron_job_id: str | None,
        source_message_id: str | None = None,
    ) -> Task:
        """Create and store the task backing a new process."""
        task_input: dict[str, Any] = {"prompt": prompt}
        if kind is TaskKind.RUN:
            task_input["title"] = title
        if cron_job_id is not None:
            task_input["cron_job_id"] = cron_job_id
            task_input["fired_at"] = utc_now().isoformat()
        if kind is TaskKind.ANSWER:
            # the parent linkage of an answer task: the user message it answers
            task_input["source_message_id"] = source_message_id
        task = Task(
            dialog_id=self._dialog.id,
            user_id=self._dialog.user_id,
            channel=self._dialog.channel,
            title=title,
            kind=kind,
            input=task_input,
            status=TaskStatus.RUNNING,
            started_at=utc_now(),
        )
        await self._tasks.add(task)
        return task

    def _start_process(self, task: Task) -> None:
        """Start the background process of an already-stored RUN task."""
        prompt = task.input.get("prompt")
        self._create_process(
            task=task,
            branch=[
                ChatMessage(role=MessageRole.SYSTEM, content=BACKGROUND_TASK_PROMPT),
                _with_date_envelope(
                    ChatMessage(
                        role=MessageRole.USER,
                        content=prompt if isinstance(prompt, str) else task.title,
                    )
                ),
            ],
            narrative_built=False,
        )

    def subscribe(self) -> asyncio.Queue[ConversationEvent]:
        """Attach a subscriber queue receiving broadcast events.

        Attaching also asks the actor to drain the outbox: results that
        terminated while no transport was attached wait there (a cron firing
        into a dialog nobody watches, the startup redelivery sweep, which runs
        before the surfaces come up). Live stream events are never replayed —
        only the outbox is.
        """
        queue: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        self._inbox.put_nowait(_Flush())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ConversationEvent]) -> None:
        """Detach a subscriber queue."""
        self._subscribers.discard(queue)

    def history(self) -> list[ChatMessage]:
        """Return a copy of the in-memory narrative (the hot tail only).

        Compacted history is not held in memory: it lives in the archive,
        reachable through summaries and history_search.
        """
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
                if self._cancellation_pending():
                    # the failure raced with a cancellation that got swallowed
                    # downstream (e.g. a store error replaces CancelledError):
                    # honor the cancel instead of looping forever
                    raise asyncio.CancelledError from None

    @staticmethod
    def _cancellation_pending() -> bool:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    async def _dispatch(self, command: _Command) -> None:
        if isinstance(command, _Submit):
            await self._handle_submit(command)
        elif isinstance(command, _ProcessTerminated):
            await self._handle_terminated(command)
        elif isinstance(command, _Flush):
            await self._flush_if_free()

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
            message_id = await self._persist(message, client_message_id=command.client_message_id)
            # the routed/narrative copy carries its row id: an answer task
            # created from this message links back via source_message_id, and
            # coverage tracking keys on it
            message = replace(message, id=message_id)
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
        """Queue the delivery of a terminated task; flush when the foreground is free.

        A live process arrives with its terminal event (`streamed` tells
        whether the user already saw it); a recovery redelivery arrives with
        no terminal and rebuilds the delivery from the stored task.
        Cancellations and user-deleted rows deliver nothing.
        """
        try:
            task = await self._tasks.get(command.task_id)
        except TaskNotFoundError:
            return  # the user deleted the row (task_delete); nothing to deliver
        if command.terminal is None:
            self._enqueue_redelivery(task)
        elif command.streamed:
            await self._mark_streamed_delivered(task)
        else:
            self._enqueue_terminal(command.terminal, task)
        await self._flush_if_free()

    def _enqueue_redelivery(self, task: Task) -> None:
        """Queue the stored outcome of a finished task for delivery (recovery path)."""
        if task.delivered_at is not None:
            return  # delivered already: redelivery is idempotent
        if task.status is TaskStatus.DONE:
            content = task.result or ""
            self._pending_deliveries.append(
                _Delivery(
                    events=(
                        TextDelta(text=content),
                        Finished(
                            message=ChatMessage(
                                role=MessageRole.ASSISTANT, content=content, task_id=task.id
                            )
                        ),
                    ),
                    task_id=task.id,
                )
            )
        elif task.status is TaskStatus.FAILED:
            self._pending_deliveries.append(
                _Delivery(
                    events=(Failed(error=task.error or DEFAULT_TASK_ERROR),),
                    task_id=task.id,
                )
            )

    def _enqueue_terminal(self, terminal: Finished | Failed, task: Task) -> None:
        """Queue a finished background process's outcome for delivery."""
        if isinstance(terminal, Finished):
            message = replace(terminal.message, task_id=task.id)
            self._pending_deliveries.append(
                _Delivery(
                    events=(
                        TextDelta(text=message.content),
                        Finished(message=message, usage=terminal.usage),
                    ),
                    task_id=task.id,
                )
            )
        else:
            self._pending_deliveries.append(
                _Delivery(events=(Failed(error=terminal.error),), task_id=task.id)
            )

    async def _mark_streamed_delivered(self, task: Task) -> None:
        """Stamp a foreground task as delivered: the user watched it stream live."""
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
            with suppress(TaskNotFoundError):  # a racing task_delete may drop the row
                await self._tasks.mark_delivered(task.id)

    async def _flush_if_free(self) -> None:
        if self._foreground() is None:
            await self._flush_deliveries()

    async def _flush_deliveries(self) -> None:
        """Broadcast queued deliveries, stamping each task delivered only after sending.

        With no subscriber attached the outbox is left alone: a broadcast into
        zero queues reaches nobody, and stamping it delivered would lose the
        result for good (`delivered_at` also stops the startup redelivery
        sweep). It drains on the next `subscribe()` instead — that is how a
        push surface such as Telegram still gets a result that finished while
        its bridge was down.

        A store failure mid-flush keeps the remaining deliveries queued (the
        already-broadcast one included): the next flush re-sends it — a
        duplicate in the transport is the accepted price of never losing a
        result.
        """
        async with self._flush_lock:
            while self._pending_deliveries and self._subscribers:
                delivery = self._pending_deliveries[0]
                accepted = 0
                for event in delivery.events:
                    accepted = self._broadcast(event)
                if accepted == 0:
                    # the terminal (last) event reached no queue: do not stamp
                    # it delivered — the delivery stays queued for a flush with
                    # a live subscriber (mirrors the no-subscriber wait above)
                    break
                if delivery.task_id is not None:
                    # a racing task_delete must not wedge the outbox behind it
                    with suppress(TaskNotFoundError):
                        await self._tasks.mark_delivered(delivery.task_id)
                self._pending_deliveries.pop(0)

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
        inject = False
        for op in ops:
            if op.action is RouteAction.CANCEL:
                if op.target_id is not None and self._cancel_process(op.target_id):
                    cancelled.add(op.target_id)
            elif op.action is RouteAction.INJECT:
                inject = True
                await self._apply_inject(message, cancelled)
            elif op.action is RouteAction.START_NEW:
                await self._apply_start_new(message, cancelled)
        if not inject and message.id is not None:
            # a package without inject is fully handled here: start_new covers
            # the message itself, bare cancel packages are pure commands
            self._covered_ids.add(message.id)

    async def _apply_inject(self, message: ChatMessage, cancelled: set[str]) -> None:
        """No-op for process control: the message already lives in the narrative.

        A running process picks it up at its next iteration sync; without a
        foreground the message needs its own process (fallback start).
        """
        if self._foreground() is not None:
            return
        await self._apply_start_new(message, cancelled)

    async def _apply_start_new(self, message: ChatMessage, cancelled: set[str]) -> None:
        async with self._spawn_lock:
            if not self._exceeds_limit(cancelled):
                await self._start_new(message)
                return
        await self._reject_for_limit(message)

    def _exceeds_limit(self, cancelled: set[str]) -> bool:
        """Whether a NEW process would exceed the limit, counting pending cancellations."""
        return len(self._processes) - len(cancelled) + 1 > self._max_processes

    async def _reject_for_limit(self, message: ChatMessage) -> None:
        """Deliver the canned process-limit notice for a refused user message."""
        notice = PROCESS_LIMIT_NOTICE_TEMPLATE.format(
            message=message.content,
            limit=self._max_processes,
            titles=self._active_titles(),
        )
        await self._deliver_notice(notice)

    async def _start_new(self, message: ChatMessage) -> None:
        """Start the foreground answer process of a narrative user message."""
        self._suspend_foreground()
        task = await self._prepare_process_task(
            message.content[:TITLE_MAX_LENGTH],
            message.content,
            kind=TaskKind.ANSWER,
            cron_job_id=None,
            source_message_id=message.id,
        )
        narrative = await self._assemble_narrative()
        process = self._create_process(
            task=task,
            branch=[self._system_message(), *narrative],
            narrative_built=True,
        )
        process.synced_len = len(process.branch)
        process.watermark = len(self._narrative)
        self._foreground_id = process.id
        if message.id is not None:
            self._covered_ids.add(message.id)

    def _system_message(self) -> ChatMessage:
        return ChatMessage(role=MessageRole.SYSTEM, content=self._prompts.get(SYSTEM_PROMPT_NAME))

    async def _assemble_narrative(self) -> list[ChatMessage]:
        """Assemble the narrative part of a branch, date-enveloping its tail copy.

        The assembled tail size also drives the memory diet: once compaction
        has advanced, everything before the hot tail is dropped from the
        in-memory narrative (S3, 2026-07-26 audit) — it stays reachable
        through the topics block and history_search, exactly like the prompt.
        """
        assembled = await self._compactor.assemble(self._dialog, self._narrative)
        self._trim_narrative(assembled.tail_count)
        narrative = assembled.messages
        if narrative:
            narrative[-1] = _with_date_envelope(narrative[-1])
        return narrative

    def _trim_narrative(self, tail_count: int) -> None:
        """Drop compacted messages from memory, keeping bookkeeping consistent.

        Watermarks are positions in the narrative list, so they shift by the
        dropped count; coverage tracking keys on message ids and only needs
        the dropped ids pruned.
        """
        drop = len(self._narrative) - tail_count
        if drop <= 0:
            return
        dropped_ids = {message.id for message in self._narrative[:drop] if message.id is not None}
        del self._narrative[:drop]
        self._covered_ids -= dropped_ids
        for process in self._processes.values():
            process.watermark = max(0, process.watermark - drop)

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
        task: Task,
        branch: list[ChatMessage],
        narrative_built: bool,
    ) -> _Process:
        process = _Process(
            id=task.id,
            title=task.title,
            task_id=task.id,
            control=LoopControl(),
            branch=branch,
            narrative_built=narrative_built,
        )
        process.pump = asyncio.create_task(self._pump_process(process))
        self._processes[process.id] = process
        return process

    async def _pump_process(self, process: _Process) -> None:
        """Stream the process loop, then always finalize and release the slot.

        Finalization writes to the store; even if that fails (or the pump is
        cancelled at shutdown), the process is removed and its termination is
        signalled so the slot is never leaked.
        """
        status = TaskStatus.FAILED
        terminal: LoopEvent = Cancelled()  # a cancelled pump (shutdown) has no terminal
        try:
            try:
                terminal = await self._stream_terminal(process)
            except Exception as exc:  # reactive compaction (store/provider) may raise
                logger.exception(
                    "process stream setup failed: dialog=%s process=%s",
                    self._dialog.id,
                    process.id,
                )
                terminal = self._fail_run(process, format_error(exc))
            try:
                status = await self._finalize(process, terminal)
            except Exception:  # a store failure must not wedge the process slot
                logger.exception(
                    "process finalize failed: dialog=%s process=%s", self._dialog.id, process.id
                )
        finally:
            self._terminate_process(process, status, terminal)

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
                if process.overflow_retried or not process.narrative_built:
                    return self._fail_run(process, format_error(exc))
                process.overflow_retried = True
                logger.info(
                    "context overflow, compacting reactively: dialog=%s process=%s",
                    self._dialog.id,
                    process.id,
                )
                if not await self._compactor.compact_now(self._dialog):
                    return self._fail_run(process, format_error(exc))
                await self._sync_branch(process, force=True)

    async def _sync_branch(self, process: _Process, *, force: bool = False) -> None:
        """Re-assemble the narrative part of the branch, keeping the private suffix.

        Runs at every iteration boundary for narrative-built processes (the
        pull model): messages appended to the narrative since the last sync —
        user messages, finals of other tasks, broker notes — become visible
        to the run. An unchanged narrative leaves the branch byte-identical
        (prefix cache), unless the sync is `force`d (reactive compaction).
        """
        if not process.narrative_built:
            return
        if not force and len(self._narrative) == process.watermark:
            return
        narrative = await self._assemble_narrative()
        private = process.branch[process.synced_len :]
        process.branch[:] = [self._system_message(), *narrative, *private]
        process.synced_len = 1 + len(narrative)
        process.watermark = len(self._narrative)

    def _fail_run(self, process: _Process, error: str) -> LoopEvent:
        """Broadcast and return a Failed terminal for the process."""
        terminal = Failed(error=error)
        if self._foreground_id == process.id:
            self._broadcast(terminal)
        return terminal

    async def _stream_once(self, process: _Process) -> LoopEvent:
        """Run the loop stream, broadcasting events only while it is the foreground."""
        context = ToolContext(
            user_id=self._dialog.user_id,
            channel=self._dialog.channel,
            dialog_id=self._dialog.id,
            task_spawner=self._spawner,
            task_deleter=self._deleter,
            owner_task_id=process.task_id,
        )
        terminal: LoopEvent = Failed(error="loop ended without a terminal event")
        try:
            async for event in self._loop.stream(process.branch, process.control, context):
                if isinstance(event, IterationStarted):
                    # pull model: re-sync the narrative part of the branch
                    # before the loop's next LLM call reads it
                    await self._sync_branch(process)
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

    def _terminate_process(
        self, process: _Process, status: TaskStatus, terminal: LoopEvent
    ) -> None:
        """Remove the process, announce completion and hand the outcome to the actor."""
        streamed = self._foreground_id == process.id
        self._remove_process(process)
        self._broadcast(
            ProcessCompleted(process_id=process.id, title=process.title, status=status.value)
        )
        self._inbox.put_nowait(
            _ProcessTerminated(
                task_id=process.task_id,
                terminal=terminal if isinstance(terminal, (Finished, Failed)) else None,
                streamed=streamed,
            )
        )
        self._requeue_unanswered(process)

    def _requeue_unanswered(self, process: _Process) -> None:
        """Re-submit user messages the process finished without seeing.

        The watermark tracks the narrative length at the process's last sync;
        newer user messages without an answer task of their own go back
        through routing, so no user message is left unanswered (this replaces
        the inject channel's drain-requeue).
        """
        if not process.narrative_built:
            return
        for message in self._narrative[process.watermark :]:
            covered = message.id is not None and message.id in self._covered_ids
            if message.role is MessageRole.USER and not covered:
                self._inbox.put_nowait(_Submit(message, recorded=True))

    async def _finalize(self, process: _Process, terminal: LoopEvent) -> TaskStatus:
        """Fold the run outcome into the narrative and the task store."""
        task = await self._tasks.get(process.task_id)
        if isinstance(terminal, Finished):
            message = replace(terminal.message, task_id=process.task_id)
            await self._persist(message, usage=terminal.usage)
            self._narrative.append(message)
            await self._tasks.mark_done(task, message.content)
            status = TaskStatus.DONE
        elif isinstance(terminal, Failed):
            await self._tasks.mark_failed(task, terminal.error)
            status = TaskStatus.FAILED
        else:
            await self._salvage_interrupted_turn(process)
            await self._tasks.mark_cancelled(task)
            status = TaskStatus.CANCELLED
        await self._report_outcome(task, status)
        return status

    async def _report_outcome(self, task: Task, status: TaskStatus) -> None:
        """Tell the outcome listener about a finished cron-tagged task, if any."""
        listener = self._task_outcome_listener
        if listener is None or "cron_job_id" not in task.input:
            return
        try:
            await listener.report_outcome(task, status)
        except Exception:  # outcome reporting must not break the dialog
            logger.exception(
                "task outcome report failed: dialog=%s task=%s", self._dialog.id, task.id
            )

    async def _salvage_interrupted_turn(self, process: _Process) -> None:
        """Keep a cancelled run's partial answer in the narrative, flagged as incomplete.

        Only the run's own messages (the private suffix) are salvageable.
        The pair is persisted atomically: the note must never be orphaned nor
        observed without the message it annotates (the compactor's tail
        snapshot relies on the pair being indivisible).
        """
        last = _latest_assistant_with_content(process.branch[process.synced_len :])
        if last is None:
            return
        salvaged = replace(last, task_id=process.task_id)
        note = ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE)
        await self._messages.append_pair(self._dialog.id, salvaged, note)
        self._narrative.extend((salvaged, note))

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

    def request_result_delivery(self, task_id: str) -> None:
        """Enqueue delivery of a finished task result via the outbox path.

        Used by startup recovery: the same command the pump enqueues when a
        live process terminates, with no terminal event — the handler then
        rebuilds the delivery from the stored task (and skips it when it was
        delivered already, so redelivery is idempotent).
        """
        self._inbox.put_nowait(_ProcessTerminated(task_id=task_id))

    async def _persist(
        self,
        message: ChatMessage,
        usage: Usage | None = None,
        client_message_id: str | None = None,
    ) -> str:
        """Persist a narrative message; return its row id."""
        return await self._messages.append(
            self._dialog.id, message, usage=usage, client_message_id=client_message_id
        )

    def _broadcast(self, event: LoopEvent) -> int:
        """Fan the event out; return how many subscriber queues accepted it.

        A slow subscriber's full queue drops stream chatter (deltas, tool
        events) but never a critical event: terminals and process markers are
        exactly what the outbox stamps `delivered_at` on — dropping one would
        lose a result for good while the store says it was delivered. For a
        critical event the oldest queued event is evicted instead, so it
        always lands.
        """
        self._seq += 1
        envelope = ConversationEvent(
            dialog_id=self._dialog.id,
            seq=self._seq,
            payload=event,
        )
        critical = isinstance(event, _CRITICAL_EVENTS)
        accepted = 0
        for queue in self._subscribers:
            try:
                queue.put_nowait(envelope)
                accepted += 1
            except asyncio.QueueFull:
                if critical and self._evict_and_put(queue, envelope):
                    accepted += 1
                self._dropped_events += 1
                logger.debug(
                    "dropped SSE event: dialog=%s seq=%s dropped_total=%s",
                    self._dialog.id,
                    self._seq,
                    self._dropped_events,
                )
        return accepted

    @staticmethod
    def _evict_and_put(
        queue: asyncio.Queue[ConversationEvent], envelope: ConversationEvent
    ) -> bool:
        """Make room for a critical event by evicting the oldest queued one."""
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:  # only full again if maxsize is 0
            return False
        return True


class _DialogTaskSpawner:
    """TaskSpawner port bound to one dialog: tasks become actor background processes."""

    def __init__(self, runner: ConversationRunner) -> None:
        self._runner = runner

    async def spawn(self, title: str, prompt: str) -> str:
        return await self._runner.spawn_task(title, prompt)


class _DialogTaskDeleter:
    """TaskDeleter port bound to one dialog: stops live task processes."""

    def __init__(self, runner: ConversationRunner) -> None:
        self._runner = runner

    async def delete(self, task_id: str) -> TaskDeleteOutcome:
        return await self._runner.delete_task(task_id)


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
        # (user_id, channel) -> the build task, memoized: concurrent callers
        # await one build, later callers get the finished runner from it
        self._builds: dict[tuple[str, str], asyncio.Task[ConversationRunner]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_runner(self, user_id: str, channel: str) -> ConversationRunner:
        """Return the live runner for (user_id, channel); the dialog is created on first contact.

        The runner narrative is rebuilt from the persisted messages, so a dialog
        survives process restarts (in-flight processes do not). The lock only
        guards the build map: initialization does DB work (the dialog row, the
        history load), and holding one global lock across it serialized every
        dialog in the process behind the slowest first contact (2026-07-26
        audit) — now only callers of the *same* dialog share a build.
        """
        key = (user_id, channel)
        async with self._lock:
            build = self._builds.get(key)
            if build is None:
                build = asyncio.create_task(self._build_runner(user_id, channel))
                self._builds[key] = build
        try:
            return await asyncio.shield(build)
        except BaseException:
            # drop the memo only when the BUILD itself died — a caller
            # cancelled mid-await (shield keeps the build running) must not
            # evict a build that other callers are about to share
            if build.done() and (build.cancelled() or build.exception() is not None):
                async with self._lock:
                    if self._builds.get(key) is build:
                        del self._builds[key]
            raise

    async def _build_runner(self, user_id: str, channel: str) -> ConversationRunner:
        dialog = await self._dialogs.get_or_create(user_id, channel)
        # only the hot slice lives in memory: everything up to the compaction
        # boundary is reachable through summaries and history_search
        boundary = await self._config.compactor.compacted_boundary(dialog.id)
        history = await self._messages.list_after(dialog.id, boundary)
        # no awaits past this point: the runner is registered in the same
        # event-loop step it starts in, so a cancelled build cannot leak a
        # started actor
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
    ) -> bool:
        """Deliver a cron firing into the user's dialog as a background process.

        Returns whether the process was actually started (see `ConversationRunner.wake`).
        """
        runner = await self.get_or_create_runner(user_id, channel)
        return await runner.wake(title, prompt, cron_job_id)

    async def recover_interrupted(self) -> None:
        """Restart orphaned tasks and redeliver undelivered results after a restart.

        Processes live in memory, so a restart strands PENDING/RUNNING tasks
        forever and loses results persisted but not yet delivered. The sweep
        runs once at startup (before the scheduler and the surfaces start) and
        never raises: a recovery failure must not take the app down — every
        operation is idempotent and simply retried on the next restart.
        """
        orphaned = await self._list_orphaned()
        for task in orphaned:
            await self._restart_orphaned(task)
        undelivered = await self._list_undelivered()
        for task in undelivered:
            await self._redeliver_undelivered(task)
        logger.info(
            "startup recovery: restarted=%s redelivered=%s", len(orphaned), len(undelivered)
        )

    async def _list_orphaned(self) -> list[Task]:
        try:
            return await self._tasks.list_orphaned()
        except Exception:
            logger.exception("orphaned task sweep failed")
            return []

    async def _list_undelivered(self) -> list[Task]:
        try:
            return await self._tasks.list_undelivered()
        except Exception:
            logger.exception("undelivered task sweep failed")
            return []

    async def _restart_orphaned(self, task: Task) -> None:
        """Restart one orphaned task as a background process of its dialog."""
        try:
            runner = await self.get_or_create_runner(task.user_id, task.channel)
            await runner.restart_task(task)
        except Exception:
            logger.exception("orphaned task restart failed: task=%s", task.id)

    async def _redeliver_undelivered(self, task: Task) -> None:
        """Redeliver a persisted-but-undelivered result via the standard path."""
        try:
            runner = await self.get_or_create_runner(task.user_id, task.channel)
            runner.request_result_delivery(task.id)
        except Exception:
            logger.exception("undelivered task redelivery failed: task=%s", task.id)

    async def stop_all(self) -> None:
        """Stop and deregister every live runner (the app is shutting down)."""
        async with self._lock:
            builds = tuple(self._builds.values())
            self._builds.clear()
            runners = tuple(self._runners.values())
            self._runners.clear()
        for build in builds:
            if not build.done():
                build.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await build
        for runner in runners:
            try:
                await runner.stop()
            except Exception:  # one failing runner must not block the shutdown
                logger.exception("runner stop failed: dialog=%s", runner.dialog_id)
