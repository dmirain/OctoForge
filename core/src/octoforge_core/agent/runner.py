"""Per-dialog actor: owns the narrative, the task processes and result delivery.

The actor is a message broker. Every process is backed by a task row
(ANSWER for user questions, RUN for deferred/cron work). A process branch
is `[system] + narrative snapshot + private working suffix`: instead of an
inject channel, the branch re-syncs its narrative part from the actor's
narrative at every iteration boundary (the pull model), so a message lands
in the narrative exactly once and every running process sees it. Every
ANSWER run owns an exchange (the durable question-obligation) and streams
its events live, tagged with that exchange — transports keep one draft per
exchange, so concurrent answers never share a message. RUN tasks work
silently and deliver whole (TextDelta + Finished / Failed) through the
outbox (`_pending_deliveries`) the moment a subscriber is attached (with
none the outbox waits — see `_flush_deliveries`). There is no report run —
delivery never involves an LLM call.
"""

import asyncio
import logging
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Protocol

from octoforge_core.agent.branch import render_branch
from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    ProcessCompleted,
    ProcessStarted,
    TextDelta,
)
from octoforge_core.agent.loop import AgentLoop, format_error
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, PromptProvider
from octoforge_core.agent.router import (
    ExchangeInfo,
    MessageRouter,
    RouteAction,
    RouteDecision,
)
from octoforge_core.context.api import INTERRUPTED_NOTE, ContextCompactor
from octoforge_core.dialogs.api import (
    DialogRepository,
    Exchange,
    ExchangeNotFoundError,
    ExchangeRepository,
    ExchangeStatus,
    MessageRepository,
)
from octoforge_core.domain import ChatMessage, Dialog, MessageKind, MessageRole
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.llm.usage import Usage
from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.store import TaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import TaskDeleteOutcome, TaskDeleter, TaskSpawner, ToolContext

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 100
# events a transport must never miss: terminals close a streamed message and
# gate `delivered_at`, process markers drive the surface's UI state. Stream
# chatter (TextDelta, tool events) may drop on a lagging subscriber instead.
_CRITICAL_EVENTS = (Finished, Failed, Cancelled, ProcessStarted, ProcessCompleted)
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
SUBMIT_FAILED_ERROR = "your message could not be saved — please send it again"
RESTART_LIMIT_ERROR = "could not resume after the service restart: process limit reached"
DEFAULT_TASK_ERROR = "unknown error"
BACKGROUND_TASK_PROMPT = (
    "You are solving a background task. User message is the task. "
    "Produce the final answer as the result."
)
DATE_ENVELOPE_TEMPLATE = "[Current date and time: {now} (UTC)]\n{content}"
CURRENT_DATE_FORMAT = "%Y-%m-%d %H:%M"
NUDGE_TEMPLATE = (
    "Кстати, я всё ещё жду ответа по «{title}» — я спрашивал: «{question}». "
    "Ответь, когда будет удобно, или скажи, что это уже неактуально."
)
# how stale an awaiting exchange must be before a new message triggers a nudge
NUDGE_AFTER_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A loop event wrapped with dialog metadata.

    `exchange_id` names the obligation the event belongs to (None for
    RUN-task deliveries and broker notices): transports keep one draft per
    exchange, so every answer streams into its own message.
    """

    dialog_id: str
    seq: int
    payload: LoopEvent
    exchange_id: str | None = None


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


def _silent_done(task: Task) -> bool:
    """Whether the task finished with a deliberately empty result (nothing to show)."""
    return task.status is TaskStatus.DONE and not (task.result or "").strip()


def _task_source_message(task: Task) -> str | None:
    """The narrative row id of the user message an ANSWER task answers."""
    raw = task.input.get("source_message_id")
    return raw if isinstance(raw, str) else None


def _exchange_outcome(status: TaskStatus) -> ExchangeStatus:
    """How a run's terminal status settles the obligation it owed."""
    if status is TaskStatus.DONE:
        return ExchangeStatus.ANSWERED
    if status is TaskStatus.CANCELLED:
        return ExchangeStatus.CANCELLED
    return ExchangeStatus.FAILED


def _task_exchange(task: Task) -> str | None:
    """The exchange an ANSWER task owes, if recorded."""
    raw: object = task.input.get("exchange_id")
    return raw if isinstance(raw, str) else None


def _task_client_source(task: Task) -> str | None:
    """The transport-level id of the task's source message, if recorded."""
    raw = task.input.get("source_client_message_id")
    return raw if isinstance(raw, str) else None


def _delivery_started(task: Task) -> ProcessStarted:
    """The delivery-opening marker: carries the reply target ahead of the text."""
    return ProcessStarted(
        process_id=task.id,
        title=task.title,
        source_client_message_id=_task_client_source(task),
    )


@dataclass(frozen=True, slots=True)
class _AnswerSource:
    """What an ANSWER task answers: the message, its transport id, its exchange."""

    message_id: str | None
    client_message_id: str | None
    exchange_id: str | None = None


class _DialogUserPrompter:
    """UserPrompter bound to one run: delivers the question, parks the exchange."""

    def __init__(self, runner: "ConversationRunner", process_id: str) -> None:
        self._runner = runner
        self._process_id = process_id

    async def ask(self, question: str) -> bool:
        return await self._runner.ask_user(self._process_id, question)


@dataclass(frozen=True, slots=True)
class _RunnerStores:
    """Persistence collaborators of one dialog actor."""

    messages: MessageRepository
    tasks: TaskStore
    exchanges: ExchangeRepository


@dataclass(frozen=True, slots=True)
class _Submit:
    """A user message to route into an exchange.

    `reply_to_exchange_id` is the transport's own resolution of an explicit
    reply (it owns its message ids; the core only sees the domain id) — the
    deterministic routing signal that needs no LLM call.
    """

    message: ChatMessage
    client_message_id: str | None = None
    reply_to_exchange_id: str | None = None
    # the runner's cancel epoch at enqueue time: a stop pressed while this
    # message was still being routed must cover it (see _start_answer)
    cancel_epoch: int = 0
    # where forwarded material came from ("Иван Петров", "канал «Ъ»"), used to
    # title the exchange that collects it; None for the user's own words
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class _Flush:
    """A fresh subscriber asking the actor to drain the delivery outbox."""


@dataclass(frozen=True, slots=True)
class _ProcessTerminated:
    """A process (or a recovery sweep) asking the actor to deliver a task outcome.

    `terminal` is the live run's Finished/Failed event (None for cancellations
    and recovery redeliveries — the stored task status decides then).
    `delivered_live` is whether that terminal actually reached a subscriber
    queue: only then is the streamed outcome stamped delivered — with nobody
    watching, it goes through the outbox like any background result.
    """

    task_id: str
    terminal: Finished | Failed | None = None
    exchange_id: str | None = None
    delivered_live: bool = False
    exchange_status: ExchangeStatus | None = None
    # a message of the same exchange arrived after the run's last sync: the
    # answer could not have accounted for it, so the exchange reopens
    unseen_messages: bool = False


@dataclass(frozen=True, slots=True, eq=False)
class _Delivery:
    """Events of one finished task awaiting transport delivery.

    Identity semantics (`eq=False`): the outbox removes a flushed delivery
    by the exact instance, never by equal payload — two look-alike
    deliveries must not collapse into one removal.
    """

    events: tuple[LoopEvent, ...]
    task_id: str | None
    exchange_id: str | None = None


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
    # the obligation this run owes the user; None for self-contained RUN tasks
    exchange_id: str | None = None
    # the user message an ANSWER process answers, and its transport-level id
    # (reply threading); both ride task.input so a restart restores them
    source_message_id: str | None = None
    source_client_message_id: str | None = None
    # the run's Finished/Failed reached at least one subscriber queue: only
    # then may the outcome be stamped delivered without outbox redelivery
    terminal_accepted: bool = False
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
        stores: _RunnerStores,
        history: list[ChatMessage],
    ) -> None:
        messages, tasks, exchanges = stores.messages, stores.tasks, stores.exchanges
        self._dialog = dialog
        self._loop = config.loop
        self._prompts = config.prompts
        self._router = config.router
        self._max_processes = config.max_processes
        self._task_outcome_listener = config.task_outcome_listener
        self._compactor = config.compactor
        self._messages = messages
        self._tasks = tasks
        self._exchanges = exchanges
        self._narrative = history
        self._processes: dict[str, _Process] = {}
        self._pending_deliveries: deque[_Delivery] = deque()
        # serializes the limit-check → process-create sequence between the
        # actor (`_apply_start_new`) and direct callers (`spawn_task`/`wake`),
        # which run in pump/scheduler tasks outside the actor's inbox
        self._spawn_lock = asyncio.Lock()
        # serializes branch assembles: the post-assemble trim shifts narrative
        # indices, so two interleaved assembles with stale snapshot coordinates
        # would evict live messages (lock order: spawn_lock -> assemble_lock)
        self._assemble_lock = asyncio.Lock()
        # bumped by every user cancel: a submit carrying an older epoch was
        # sent before the stop, so the stop covers it even if it was still
        # inside the router when the button was pressed
        self._cancel_epoch = 0
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

    async def submit(
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_exchange_id: str | None = None,
        kind: MessageKind = MessageKind.OWN,
        origin: str | None = None,
    ) -> None:
        """Submit a user message; the router decides which exchange it joins.

        `client_message_id` is an idempotency key: a repeat with an
        already-recorded key is skipped (delivery retries are normal).
        `reply_to_exchange_id` lets a transport that knows the user replied to
        a specific message name the exchange outright, skipping the router.
        `kind=MATERIAL` marks content the user shared rather than wrote (a
        forward): it never opens an obligation and never starts a run —
        `origin` describes where it came from, for the title of the exchange
        that eventually collects it.
        """
        await self._inbox.put(
            _Submit(
                ChatMessage(role=MessageRole.USER, content=content, kind=kind),
                client_message_id=client_message_id,
                reply_to_exchange_id=reply_to_exchange_id,
                cancel_epoch=self._cancel_epoch,
                origin=origin,
            )
        )

    async def cancel(self) -> None:
        """Cancel every live answer run (explicit user request).

        Applied directly, not through the inbox: the actor may be busy inside
        a routing LLM call (up to OF_ROUTER_TIMEOUT_SECONDS), and a queued
        cancel would wait behind it — a stop must act now. Live runs are
        stopped via their LoopControl flags (owned by this event loop);
        parked AWAITING_USER exchanges with no live run are closed too —
        otherwise the nudge would keep re-asking a question the user just
        stopped caring about.
        """
        self._handle_cancel()
        await self._cancel_parked_exchanges()

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
        """Restart a task orphaned by a service restart.

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
        await self._flush_deliveries()

    async def _start_orphaned(self, task: Task) -> None:
        """Start the replacement background process of an orphaned task."""
        if task.kind is not TaskKind.ANSWER:
            self._start_process(task)
            return
        exchange_id = _task_exchange(task)
        if exchange_id is not None and await self._exchange_awaits_user(exchange_id):
            # the run died between ask_user and its finalization: the ask
            # already went out and the exchange waits for the user — a
            # restarted run would clobber that state and duplicate the work.
            # Close the row silently; the user's reply resumes the exchange.
            await self._tasks.mark_done(task, "")
            return
        narrative, watermark = await self._assemble_narrative(own_exchange_id=exchange_id)
        process = self._create_process(
            task=task,
            branch=[self._system_message(), *narrative],
            narrative_built=True,
        )
        process.synced_len = len(process.branch)
        process.watermark = watermark
        if exchange_id is not None:
            # the restarted run re-owns its obligation
            with suppress(ExchangeNotFoundError):
                await self._exchanges.set_status(
                    exchange_id, ExchangeStatus.IN_PROGRESS, owner_task_id=task.id
                )

    async def _exchange_awaits_user(self, exchange_id: str) -> bool:
        try:
            exchange = await self._exchanges.get(exchange_id)
        except ExchangeNotFoundError:
            return False
        return exchange.status is ExchangeStatus.AWAITING_USER

    def _spawn_refusal(self) -> str:
        return SPAWN_REFUSAL_TEMPLATE.format(
            limit=self._max_processes, titles=self._active_titles()
        )

    async def _publish_cron_limit_note(self, title: str) -> None:
        notice = CRON_LIMIT_NOTICE_TEMPLATE.format(
            title=title, limit=self._max_processes, titles=self._active_titles()
        )
        await self._deliver_notice(notice)

    async def _deliver_notice(self, content: str, exchange_id: str | None = None) -> None:
        """Persist a broker message (limit notice, nudge, question) and queue it.

        A question of an exchange jumps the queue: it is the only thing that
        unblocks that obligation, so it must not wait behind other results.
        """
        notice = ChatMessage(role=MessageRole.ASSISTANT, content=content, exchange_id=exchange_id)
        message_id = await self._persist(notice)
        notice = replace(notice, id=message_id)
        self._narrative.append(notice)
        delivery = _Delivery(
            events=(TextDelta(text=content), Finished(message=notice)),
            task_id=None,
            exchange_id=exchange_id,
        )
        if exchange_id is not None:
            self._pending_deliveries.appendleft(delivery)
        else:
            self._pending_deliveries.append(delivery)
        await self._flush_deliveries()

    async def _prepare_process_task(
        self,
        title: str,
        prompt: str,
        *,
        kind: TaskKind,
        cron_job_id: str | None,
        source: _AnswerSource | None = None,
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
            task_input["source_message_id"] = source.message_id if source else None
            if source is not None and source.client_message_id is not None:
                # the transport-level id of that message (reply threading)
                task_input["source_client_message_id"] = source.client_message_id
            if source is not None and source.exchange_id is not None:
                # the obligation this run owes: restored verbatim after a restart
                task_input["exchange_id"] = source.exchange_id
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
                if isinstance(command, _Submit):
                    # the message may be persisted yet unrouted/unanswered: a
                    # silent black hole left users waiting forever — tell
                    # them. A resend can duplicate the row (duplicate beats
                    # silence); the unowned-open sweep revives an exchange
                    # stranded mid-apply.
                    self._broadcast(Failed(error=SUBMIT_FAILED_ERROR))

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
            await self._flush_deliveries()

    async def _handle_submit(self, command: _Submit) -> None:
        message = command.message
        try:
            if await self._is_duplicate(command.client_message_id):
                logger.info(
                    "duplicate submit skipped: dialog=%s key=%s",
                    self._dialog.id,
                    command.client_message_id,
                )
                return
            message_id = await self._persist(message, client_message_id=command.client_message_id)
        except Exception:
            # a store failure here silently swallowed the user's message
            # (the actor's catch only logged it): tell the transport, so
            # the user knows to resend instead of waiting for an answer
            logger.exception("submit persist failed: dialog=%s", self._dialog.id)
            self._broadcast(Failed(error=SUBMIT_FAILED_ERROR))
            return
        # the routed/narrative copy carries its row id: the answer task links
        # back to it via source_message_id
        message = replace(message, id=message_id)
        if message.kind is MessageKind.MATERIAL:
            await self._collect_material(message, command)
            return
        decision = await self._route(message, command)
        await self._apply_route(message, decision, command)

    async def _collect_material(self, message: ChatMessage, command: _Submit) -> None:
        """Take in forwarded material: it owes nothing, so nothing starts.

        Material is someone else's text the user shared. It enters the
        narrative as context and never opens an obligation — the reaction
        comes later, when the user asks something or the collection settles.
        """
        self._narrative.append(message)
        logger.info(
            "material collected: dialog=%s message=%s origin=%s",
            self._dialog.id,
            message.id,
            command.origin,
        )

    async def _route(self, message: ChatMessage, command: _Submit) -> RouteDecision:
        """Decide which exchange the message belongs to (deterministic first).

        Two shortcuts skip the LLM entirely: an explicit transport-level reply
        names its exchange outright, and with no live exchange there is
        nothing to belong to. Everything else goes to the router, which sees
        exchanges (user-visible obligations), not process ids.
        """
        live = await self._exchanges.list_live(self._dialog.id)
        live_ids = {item.id for item in live}
        if command.reply_to_exchange_id in live_ids:
            logger.info(
                "routed by reply: dialog=%s exchange=%s",
                self._dialog.id,
                command.reply_to_exchange_id,
            )
            return RouteDecision(
                action=RouteAction.CONTINUE, exchange_id=command.reply_to_exchange_id
            )
        if not live:
            return RouteDecision()
        infos = tuple(
            ExchangeInfo(
                id=item.id,
                title=item.title,
                status=item.status,
                pending_question=item.pending_question,
                # staleness, not lifetime: "how long since we last touched
                # it" is what a routing decision needs (matches the nudge)
                age_seconds=(utc_now() - item.updated_at).total_seconds(),
            )
            for item in live
        )
        return await self._router.route(infos, message.content, self._max_processes)

    async def ask_user(self, process_id: str, question: str) -> bool:
        """Deliver a run's clarifying question and park its exchange.

        Called from the `ask_user` tool inside a pump task. The obligation is
        NOT closed: it moves to AWAITING_USER, so the "what is left to do"
        predicate skips it (that is the user's move) while it stays visible
        to the agent, the reminder and the operator console. Returns whether
        the question was actually delivered: a RUN/cron process has no
        exchange to park, so asking is unavailable there — the tool must
        report that honestly instead of promising a reply.
        """
        process = self._processes.get(process_id)
        if process is None or process.exchange_id is None:
            return False
        await self._deliver_notice(question, exchange_id=process.exchange_id)
        with suppress(ExchangeNotFoundError):
            # keep ownership: the asking run is still alive (it unwinds on
            # its own schedule after the ack) — clearing the owner here let
            # a prompt reply spawn a second run on the same exchange, both
            # streaming into one transport draft
            await self._exchanges.set_status(
                process.exchange_id,
                ExchangeStatus.AWAITING_USER,
                owner_task_id=process.task_id,
                pending_question=question,
            )
        return True

    async def _is_duplicate(self, client_message_id: str | None) -> bool:
        """Whether a submit with this idempotency key was already recorded."""
        if client_message_id is None:
            return False
        return await self._messages.find_by_client_id(self._dialog.id, client_message_id)

    async def _cancel_parked_exchanges(self) -> None:
        """Close AWAITING_USER exchanges whose run already ended (stop button).

        Exchanges with a live run are settled CANCELLED by their own
        termination; OPEN ones are left alone — they are transient (a run
        is being started, or the sweep will pick them up) and cancelling
        them here would race the actor's start path.
        """
        for exchange in await self._exchanges.list_live(self._dialog.id):
            if (
                exchange.status is ExchangeStatus.AWAITING_USER
                and exchange.owner_task_id not in self._processes
            ):
                await self._exchanges.set_status(exchange.id, ExchangeStatus.CANCELLED)

    def _handle_cancel(self) -> None:
        """Stop every live answer run (the user's stop button / /cancel).

        RUN tasks (spawned/cron work) keep going: they are background jobs
        with their own lifecycle, cancellable via task_delete or the router.
        Bumping the epoch covers the message the actor is still routing: its
        run starts already-cancelled instead of answering a stopped request.
        """
        self._cancel_epoch += 1
        for process in self._processes.values():
            if process.exchange_id is not None:
                process.control.cancel()

    async def _handle_terminated(self, command: _ProcessTerminated) -> None:
        """Settle the run's exchange, then queue or stamp the task outcome.

        A live answer run streamed its outcome (its exchange tag says so) and
        is only stamped delivered; a RUN task's terminal is queued whole; a
        recovery redelivery arrives with no terminal and rebuilds the
        delivery from the stored task. Cancellations and user-deleted rows
        deliver nothing.
        """
        await self._settle_exchange(command)
        try:
            task = await self._tasks.get(command.task_id)
        except TaskNotFoundError:
            return  # the user deleted the row (task_delete); nothing to deliver
        if _silent_done(task):
            # a deliberately empty result: stamp delivered, never enqueue —
            # otherwise the startup sweep would redeliver emptiness forever
            await self._mark_streamed_delivered(task)
        elif command.terminal is None:
            self._enqueue_redelivery(task)
        elif command.exchange_id is not None and command.delivered_live:
            await self._mark_streamed_delivered(task)
        else:
            # either a RUN task (never streamed) or a streamed answer whose
            # terminal reached zero subscribers (tab reload at the wrong
            # moment, bridge down): the outbox redelivers it whole
            self._enqueue_terminal(command.terminal, task)
        await self._flush_deliveries()
        # a slot just freed: revive anything stranded OPEN without an owner
        await self._sweep_unowned_open()

    async def _settle_exchange(self, command: _ProcessTerminated) -> None:
        """Move the finished run's exchange to its next state.

        Order matters: the owner guard first (a settled exchange that
        changed hands belongs to its new owner), then the unseen-messages
        reopen (a reply that arrived during the run's tail must get a fresh
        run — even when the run had asked and parked the exchange), then the
        AWAITING_USER handling (kept open for the user's move, closed on a
        user cancel or when the run answered anyway).
        """
        if command.exchange_id is None or command.exchange_status is None:
            return
        try:
            exchange = await self._exchanges.get(command.exchange_id)
        except ExchangeNotFoundError:
            return
        logger.info(
            "settling exchange: dialog=%s exchange=%s from=%s to=%s unseen=%s",
            self._dialog.id,
            command.exchange_id,
            exchange.status.value,
            command.exchange_status.value,
            command.unseen_messages,
        )
        if exchange.owner_task_id != command.task_id:
            # the exchange changed hands while this termination sat in the
            # inbox (a follow-up already spawned a fresh owner, or a cancel
            # cleared it): the new owner settles it — touching it here would
            # clobber a live run's ownership or resurrect a cancelled exchange
            logger.info(
                "settle skipped, exchange changed hands: dialog=%s exchange=%s task=%s owner=%s",
                self._dialog.id,
                exchange.id,
                command.task_id,
                exchange.owner_task_id,
            )
            return
        if command.unseen_messages and command.exchange_status is not ExchangeStatus.CANCELLED:
            # a message of this exchange arrived after the run's last sync —
            # even one the run asked for (AWAITING_USER): reopen, fresh run.
            # A user-cancelled run must NOT resurrect itself this way: the
            # stop came after the message, so the stop covers it.
            await self._exchanges.set_status(exchange.id, ExchangeStatus.OPEN)
            await self._resume_open_exchange(exchange.id)
            return
        if exchange.status is ExchangeStatus.AWAITING_USER:
            await self._settle_awaiting(exchange, command)
            return
        await self._exchanges.set_status(exchange.id, command.exchange_status)

    async def _settle_awaiting(self, exchange: Exchange, command: _ProcessTerminated) -> None:
        """Settle a run that asked the user something and then terminated.

        The obligation normally stays open (the reply resumes it). Two
        exceptions: a user cancel closes it — otherwise the nudge would
        re-ask a question the user explicitly stopped — and a run that
        asked but then answered anyway closes it as answered, or its stale
        pending question would be nudged forever.
        """
        if command.exchange_status is ExchangeStatus.CANCELLED:
            await self._exchanges.set_status(exchange.id, ExchangeStatus.CANCELLED)
            return
        answered_anyway = (
            command.exchange_status is ExchangeStatus.ANSWERED
            and isinstance(command.terminal, Finished)
            and bool(command.terminal.message.content.strip())
        )
        if answered_anyway:
            await self._exchanges.set_status(exchange.id, ExchangeStatus.ANSWERED)

    async def _resume_open_exchange(self, exchange_id: str, *, notify_limit: bool = True) -> None:
        """Give an OPEN exchange a fresh run (its last one missed something)."""
        message = next(
            (
                item
                for item in reversed(self._narrative)
                if item.role is MessageRole.USER and item.exchange_id == exchange_id
            ),
            None,
        )
        if message is None:
            # compacted out of the hot tail: nothing to hand a fresh run
            logger.warning(
                "cannot resume exchange, its message left the hot tail: dialog=%s exchange=%s",
                self._dialog.id,
                exchange_id,
            )
            return
        with suppress(ExchangeNotFoundError):
            async with self._spawn_lock:
                # re-read INSIDE the lock: a sweep and a settle may both try
                # to revive the same exchange — the second must see the first
                exchange = await self._exchanges.get(exchange_id)
                if exchange.status is not ExchangeStatus.OPEN or exchange.owner_task_id is not None:
                    return
                if self._exceeds_limit(set()):
                    if notify_limit:
                        # the user-facing path tells them; the background
                        # sweep stays silent and retries on the next slot
                        await self._reject_for_limit(message)
                    return
                await self._start_answer(exchange, message)

    async def _sweep_unowned_open(self) -> None:
        """Revive OPEN exchanges nobody owns (crash and limit leftovers).

        The documented predicate "OPEN without an owner = work for the
        system", run when a slot frees up (and via `resume_stranded` at
        startup). Silent on the process limit: the next freed slot retries.
        """
        try:
            stranded = await self._exchanges.list_unowned_open(self._dialog.id)
        except Exception:  # the sweep is a safety net; never break the actor
            logger.exception("unowned-open sweep failed: dialog=%s", self._dialog.id)
            return
        for exchange in stranded:
            await self._resume_open_exchange(exchange.id, notify_limit=False)

    async def resume_stranded(self) -> None:
        """Startup entry of the unowned-open sweep (called by the manager)."""
        await self._sweep_unowned_open()

    def _enqueue_redelivery(self, task: Task) -> None:
        """Queue the stored outcome of a finished task for delivery (recovery path)."""
        if task.delivered_at is not None:
            return  # delivered already: redelivery is idempotent
        if task.status is TaskStatus.DONE:
            content = task.result or ""
            self._pending_deliveries.append(
                _Delivery(
                    events=(
                        _delivery_started(task),
                        TextDelta(text=content),
                        Finished(
                            message=ChatMessage(
                                role=MessageRole.ASSISTANT, content=content, task_id=task.id
                            ),
                            source_client_message_id=_task_client_source(task),
                        ),
                    ),
                    task_id=task.id,
                    exchange_id=_task_exchange(task),
                )
            )
        elif task.status is TaskStatus.FAILED:
            self._pending_deliveries.append(
                _Delivery(
                    events=(
                        _delivery_started(task),
                        Failed(error=task.error or DEFAULT_TASK_ERROR),
                    ),
                    task_id=task.id,
                    exchange_id=_task_exchange(task),
                )
            )

    def _enqueue_terminal(self, terminal: Finished | Failed, task: Task) -> None:
        """Queue a finished background process's outcome for delivery."""
        if isinstance(terminal, Finished):
            message = replace(terminal.message, task_id=task.id)
            self._pending_deliveries.append(
                _Delivery(
                    events=(
                        _delivery_started(task),
                        TextDelta(text=message.content),
                        Finished(
                            message=message,
                            usage=terminal.usage,
                            source_client_message_id=terminal.source_client_message_id,
                        ),
                    ),
                    task_id=task.id,
                    exchange_id=_task_exchange(task),
                )
            )
        else:
            self._pending_deliveries.append(
                _Delivery(
                    events=(_delivery_started(task), Failed(error=terminal.error)),
                    task_id=task.id,
                    exchange_id=_task_exchange(task),
                )
            )

    async def _mark_streamed_delivered(self, task: Task) -> None:
        """Stamp a task as delivered: the user already watched it stream live."""
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
            with suppress(TaskNotFoundError):  # a racing task_delete may drop the row
                await self._tasks.mark_delivered(task.id)

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
                    accepted = self._broadcast(event, exchange_id=delivery.exchange_id)
                if accepted == 0:
                    # the terminal (last) event reached no queue: do not stamp
                    # it delivered — the delivery stays queued for a flush with
                    # a live subscriber (mirrors the no-subscriber wait above)
                    break
                if delivery.task_id is not None:
                    # a racing task_delete must not wedge the outbox behind it
                    with suppress(TaskNotFoundError):
                        await self._tasks.mark_delivered(delivery.task_id)
                # remove by identity, not position: an ask_user question may
                # have jumped the queue (appendleft) during the await above —
                # a blind popleft would silently drop that question instead
                if self._pending_deliveries and self._pending_deliveries[0] is delivery:
                    self._pending_deliveries.popleft()
                else:
                    with suppress(ValueError):
                        self._pending_deliveries.remove(delivery)

    async def _apply_route(
        self, message: ChatMessage, decision: RouteDecision, command: _Submit
    ) -> None:
        """Attach the message to its exchange and make sure someone owes an answer.

        The message becomes visible to running branches only here, once its
        fate is settled: a branch that saw it mid-routing could neither know
        whether it was a clarification nor whether another run was about to
        own it (that window cost a lost answer and a duplicated one in
        production, 28.07).
        """
        cancelled = await self._cancel_exchanges(decision.cancel_ids)
        exchange_id: str | None = None
        refused = False
        if decision.action is RouteAction.CONTINUE and decision.exchange_id is not None:
            exchange_id = decision.exchange_id
        elif decision.action is not RouteAction.COMMAND:
            if self._exceeds_limit(self._cancelled_tasks(cancelled)):
                refused = True
            else:
                exchange = await self._exchanges.create(self._dialog.id, message.content)
                exchange_id = exchange.id
        # the user's message enters the narrative before anything reacts to it
        message = replace(message, exchange_id=exchange_id)
        self._narrative.append(message)
        if message.id is not None and exchange_id is not None:
            await self._messages.set_exchange(message.id, exchange_id)
        if refused:
            await self._reject_for_limit(message)
            return
        # owner first, nudge after: a cosmetic reminder must never sit
        # between exchange creation and owner assignment — a failure inside
        # it would strand the fresh exchange unowned
        if exchange_id is not None:
            await self._ensure_owner(
                exchange_id,
                message,
                command.client_message_id,
                cancelled,
                cancel_epoch=command.cancel_epoch,
            )
        await self._nudge_stale_exchanges(exchange_id)

    async def _cancel_exchanges(self, exchange_ids: tuple[str, ...]) -> set[str]:
        """Cancel the named exchanges and their live runs; return what was cancelled."""
        cancelled: set[str] = set()
        for exchange_id in exchange_ids:
            with suppress(ExchangeNotFoundError):
                exchange = await self._exchanges.get(exchange_id)
                if exchange.owner_task_id is not None:
                    self._cancel_process(exchange.owner_task_id)
                await self._exchanges.set_status(exchange_id, ExchangeStatus.CANCELLED)
                cancelled.add(exchange_id)
        return cancelled

    async def _ensure_owner(
        self,
        exchange_id: str,
        message: ChatMessage,
        client_key: str | None,
        cancelled: set[str] | None = None,
        cancel_epoch: int | None = None,
    ) -> None:
        """Make sure a live run owes this exchange an answer.

        An exchange already being answered needs nothing: its run pulls the
        new message in at the next iteration sync (the pull model). One that
        is open or was waiting for the user gets a run now. `cancelled` are
        the exchanges the same routing decision just cancelled: their runs
        unwind cooperatively, so the limit check must discount them or a
        "stop X, continue Y" message hits the limit on X's still-live slot.
        """
        with suppress(ExchangeNotFoundError):
            async with self._spawn_lock:
                # read the exchange INSIDE the lock: a check-then-act across
                # the lock await is how a second owner slips in
                exchange = await self._exchanges.get(exchange_id)
                if exchange.owner_task_id is not None and exchange.owner_task_id in self._processes:
                    return
                if self._exceeds_limit(self._cancelled_tasks(cancelled or set())):
                    await self._reject_for_limit(message)
                    return
                await self._start_answer(exchange, message, client_key, cancel_epoch=cancel_epoch)

    async def _nudge_stale_exchanges(self, current_exchange_id: str | None) -> None:
        """Remind the user about a question they left hanging (event-driven).

        Fires when a new message arrives while some other exchange has been
        waiting for a reply longer than `NUDGE_AFTER_SECONDS` — as its own
        message, quoting the agent's own pending question.
        """
        now = utc_now()
        for exchange in await self._exchanges.list_live(self._dialog.id):
            if (
                exchange.id == current_exchange_id
                or exchange.status is not ExchangeStatus.AWAITING_USER
                or exchange.pending_question is None
                or (now - exchange.updated_at).total_seconds() < NUDGE_AFTER_SECONDS
            ):
                continue
            await self._deliver_notice(
                NUDGE_TEMPLATE.format(title=exchange.title, question=exchange.pending_question)
            )

    def _exceeds_limit(self, cancelled: set[str]) -> bool:
        """Whether a NEW process would exceed the limit, counting pending cancellations."""
        return len(self._processes) - len(cancelled) + 1 > self._max_processes

    def _cancelled_tasks(self, cancelled: set[str]) -> set[str]:
        """The just-cancelled exchanges whose runs still occupy a slot.

        Cancellation is cooperative: the pump unwinds on its own schedule, so
        a cancelled run may still sit in `_processes` when the same routing
        decision needs a slot. Only those count as a discount — a cancelled
        exchange whose run already ended (or never had one) frees nothing.
        """
        live_exchanges = {
            process.exchange_id
            for process in self._processes.values()
            if process.exchange_id is not None
        }
        return cancelled & live_exchanges

    async def _reject_for_limit(self, message: ChatMessage) -> None:
        """Deliver the canned process-limit notice for a refused user message."""
        notice = PROCESS_LIMIT_NOTICE_TEMPLATE.format(
            message=message.content,
            limit=self._max_processes,
            titles=self._active_titles(),
        )
        await self._deliver_notice(notice)

    async def _start_answer(
        self,
        exchange: Exchange,
        message: ChatMessage,
        client_key: str | None = None,
        cancel_epoch: int | None = None,
    ) -> None:
        """Start the run that owes `exchange` an answer.

        Every answer run streams live into its own per-exchange message;
        starting one never disturbs the others. A run whose submit predates
        the last user cancel starts already-cancelled: the stop button was
        pressed while the message was still being routed, and "I pressed
        stop and it answered anyway" is a broken stop.
        """
        task = await self._prepare_process_task(
            exchange.title,
            message.content,
            kind=TaskKind.ANSWER,
            cron_job_id=None,
            source=_AnswerSource(
                message_id=message.id,
                client_message_id=client_key,
                exchange_id=exchange.id,
            ),
        )
        await self._exchanges.set_status(
            exchange.id, ExchangeStatus.IN_PROGRESS, owner_task_id=task.id
        )
        narrative, watermark = await self._assemble_narrative(own_exchange_id=exchange.id)
        process = self._create_process(
            task=task,
            branch=[self._system_message(), *narrative],
            narrative_built=True,
        )
        process.synced_len = len(process.branch)
        process.watermark = watermark
        if cancel_epoch is not None and cancel_epoch < self._cancel_epoch:
            process.control.cancel()
        # the reply target must reach the transport BEFORE the first token:
        # a reply can only be set when the message is created. Every answer
        # run streams into its own per-exchange draft — there is no
        # foreground slot to wait for.
        self._broadcast(
            ProcessStarted(
                process_id=process.id,
                title=process.title,
                source_client_message_id=process.source_client_message_id,
            ),
            exchange_id=exchange.id,
        )

    def _system_message(self) -> ChatMessage:
        return ChatMessage(role=MessageRole.SYSTEM, content=self._prompts.get(SYSTEM_PROMPT_NAME))

    async def _assemble_narrative(
        self, own_exchange_id: str | None = None
    ) -> tuple[list[ChatMessage], int]:
        """Assemble the narrative part of a branch: compactor tail, marks, date.

        Returns the rendered narrative AND the watermark the caller must
        record for the branch: the post-trim index up to which the branch
        has seen the narrative. Both come from the compactor's snapshot —
        a message appended while the assemble awaited is NOT in the branch,
        so it must stay above the watermark or the pull model silently
        loses it (the run never syncs it in, `_has_unseen_messages` never
        reopens for it, and the exchange closes as answered).

        `_assemble_lock` serializes assembles across pumps: the trim below
        shifts narrative indices, and a concurrent assemble holding stale
        snapshot coordinates would evict live messages.

        The assembled tail size also drives the memory diet: once compaction
        has advanced, everything before the hot tail is dropped from the
        in-memory narrative (S3, 2026-07-26 audit) — it stays reachable
        through the topics block and history_search, exactly like the prompt.

        Marking is delegated to `render_branch`: one rule over durable
        exchange state instead of the envelope patchwork it replaced.
        """
        async with self._assemble_lock:
            assembled = await self._compactor.assemble(self._dialog, self._narrative)
            self._trim_narrative(assembled.snapshot_len - assembled.tail_count)
        narrative = render_branch(
            assembled.messages, own_exchange_id, await self._live_exchange_ids(own_exchange_id)
        )
        if narrative:
            narrative[-1] = _with_date_envelope(narrative[-1])
        return narrative, assembled.tail_count

    async def _live_exchange_ids(self, own_exchange_id: str | None = None) -> frozenset[str]:
        """Other unanswered obligations, from the durable state.

        Read from the store, not from the in-memory processes: an exchange
        exists before its message becomes visible and before its run starts,
        so the process map would leave a window in which another run sees a
        question that looks unclaimed.
        """
        return frozenset(
            item.id
            for item in await self._exchanges.list_live(self._dialog.id)
            if item.id != own_exchange_id
        )

    def _trim_narrative(self, drop: int) -> None:
        """Drop compacted messages from memory, keeping watermarks consistent.

        `drop` comes from the compactor's snapshot (snapshot_len - tail_count),
        NOT from the live list length: messages appended during the assemble
        are past the snapshot and must survive the trim. Watermarks are
        positions in the narrative list, so they shift by the dropped count.
        """
        if drop <= 0:
            return
        del self._narrative[:drop]
        for process in self._processes.values():
            process.watermark = max(0, process.watermark - drop)

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
            source_message_id=_task_source_message(task),
            source_client_message_id=_task_client_source(task),
            exchange_id=_task_exchange(task),
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
        clarifications of this run's exchange, finals of other runs, broker
        notes — become visible, each carrying its role (see `render_branch`).
        An unchanged narrative leaves the branch byte-identical (prefix
        cache), unless the sync is `force`d (reactive compaction).
        """
        if not process.narrative_built:
            return
        if not force and len(self._narrative) == process.watermark:
            return
        narrative, watermark = await self._assemble_narrative(own_exchange_id=process.exchange_id)
        private = process.branch[process.synced_len :]
        process.branch[:] = [self._system_message(), *narrative, *private]
        process.synced_len = 1 + len(narrative)
        # the snapshot watermark, NOT len(self._narrative): a message appended
        # during the assemble is not in this branch and must stay unseen
        process.watermark = watermark

    def _fail_run(self, process: _Process, error: str) -> LoopEvent:
        """Broadcast and return a Failed terminal for the process."""
        terminal = Failed(error=error)
        if process.exchange_id is not None:
            process.terminal_accepted = (
                self._broadcast(terminal, exchange_id=process.exchange_id) > 0
            )
        return terminal

    async def _stream_once(self, process: _Process) -> LoopEvent:
        """Run the loop once, streaming an answer run's events under its exchange tag."""
        context = ToolContext(
            user_id=self._dialog.user_id,
            channel=self._dialog.channel,
            dialog_id=self._dialog.id,
            task_spawner=self._spawner,
            task_deleter=self._deleter,
            user_prompter=_DialogUserPrompter(self, process.id),
            owner_task_id=process.task_id,
        )
        terminal: LoopEvent = Failed(error="loop ended without a terminal event")
        try:
            async for event in self._loop.stream(process.branch, process.control, context):
                if isinstance(event, IterationStarted):
                    # pull model: re-sync the narrative part of the branch
                    # before the loop's next LLM call reads it
                    await self._sync_branch(process)
                out: LoopEvent = event
                if isinstance(event, Finished):
                    # reply threading: the loop knows nothing about tasks
                    out = replace(event, source_client_message_id=process.source_client_message_id)
                if process.exchange_id is not None:
                    accepted = self._broadcast(out, exchange_id=process.exchange_id)
                    if isinstance(out, (Finished, Failed)):
                        # delivery is only real if the terminal reached a
                        # subscriber queue; "streamed live" alone proves
                        # nothing when nobody was watching
                        process.terminal_accepted = accepted > 0
                if isinstance(out, (Finished, Cancelled, Failed)):
                    terminal = out
        except ContextOverflowError:
            raise  # the reactive-compaction retry handles it one level up
        except Exception as exc:  # loop failures are broadcast, not raised
            logger.exception(
                "process loop crashed: dialog=%s process=%s", self._dialog.id, process.id
            )
            terminal = Failed(error=format_error(exc))
            if process.exchange_id is not None:
                accepted = self._broadcast(terminal, exchange_id=process.exchange_id)
                process.terminal_accepted = accepted > 0
        return terminal

    def _terminate_process(
        self, process: _Process, status: TaskStatus, terminal: LoopEvent
    ) -> None:
        """Remove the process, announce completion and hand the outcome to the actor."""
        logger.info(
            "process terminated: dialog=%s task=%s exchange=%s status=%s",
            self._dialog.id,
            process.task_id,
            process.exchange_id,
            status.value,
        )
        self._remove_process(process)
        self._broadcast(
            ProcessCompleted(process_id=process.id, title=process.title, status=status.value),
            exchange_id=process.exchange_id,
        )
        self._inbox.put_nowait(
            _ProcessTerminated(
                task_id=process.task_id,
                terminal=terminal if isinstance(terminal, (Finished, Failed)) else None,
                exchange_id=process.exchange_id,
                delivered_live=process.terminal_accepted,
                exchange_status=_exchange_outcome(status),
                unseen_messages=self._has_unseen_messages(process),
            )
        )

    def _has_unseen_messages(self, process: _Process) -> bool:
        """Whether messages of this run's exchange arrived after its last sync.

        Replaces the requeue heuristic: instead of re-submitting messages, the
        exchange simply goes back to OPEN and gets a fresh run — the durable
        state decides, not an in-memory scan.
        """
        if not process.narrative_built or process.exchange_id is None:
            return False
        return any(
            message.role is MessageRole.USER and message.exchange_id == process.exchange_id
            for message in self._narrative[process.watermark :]
        )

    async def _finalize(self, process: _Process, terminal: LoopEvent) -> TaskStatus:
        """Fold the run outcome into the narrative and the task store.

        An empty final is a process choosing silence (e.g. its question was
        taken over by another process, or the user said to drop it): the task
        completes, but no empty bubble enters the narrative and nothing is
        delivered — an undelivered empty result would otherwise be redelivered
        by the startup sweep forever (see _handle_terminated's silent-done stamp).
        """
        task = await self._tasks.get(process.task_id)
        if isinstance(terminal, Finished):
            if not terminal.message.content.strip():
                # silence is legitimate but must never be invisible: it once
                # masked a queued answer that could not tell what to answer
                logger.info(
                    "process finished with an empty final: dialog=%s task=%s title=%r",
                    self._dialog.id,
                    process.task_id,
                    process.title,
                )
            if terminal.message.content.strip():
                message = replace(
                    terminal.message,
                    task_id=process.task_id,
                    exchange_id=process.exchange_id,
                )
                await self._persist(message, usage=terminal.usage)
                self._narrative.append(message)
                if not process.narrative_built:
                    # RUN/cron results grow the narrative too, but only
                    # answer runs assemble branches — a dialog fed purely by
                    # cron would never trigger compaction and grow unbounded
                    await self._compact_after_run_final()
            await self._tasks.mark_done(task, terminal.message.content)
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

    async def _compact_after_run_final(self) -> None:
        """Run the compactor's overflow check for a narrative grown by RUN work.

        Reuses the assemble path (which carries the trigger and the memory
        trim) and discards the rendered result. Failures are swallowed: this
        is bookkeeping, not the run's outcome.
        """
        try:
            await self._assemble_narrative()
        except Exception:
            logger.exception("post-run compaction check failed: dialog=%s", self._dialog.id)

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

    def _broadcast(self, event: LoopEvent, exchange_id: str | None = None) -> int:
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
            exchange_id=exchange_id,
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
        """Make room for a critical event by evicting the oldest DROPPABLE one.

        Blind head-eviction could evict an earlier critical event (a terminal
        whose delivery the store already recorded) to admit a later one. The
        queue is drained, the oldest non-critical entry is dropped, order is
        preserved. With nothing droppable the put is refused: the caller
        counts the event as not accepted, so the outbox keeps the delivery
        queued instead of stamping it delivered.
        """
        drained: list[ConversationEvent] = []
        while True:
            try:
                drained.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        victim = next(
            (i for i, item in enumerate(drained) if not isinstance(item.payload, _CRITICAL_EVENTS)),
            None,
        )
        if victim is not None:
            del drained[victim]
            drained.append(envelope)
        for item in drained:  # order preserved; refused put restores untouched
            with suppress(asyncio.QueueFull):
                queue.put_nowait(item)
        return victim is not None


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
        exchanges: ExchangeRepository,
    ) -> None:
        self._config = config
        self._dialogs = dialogs
        self._messages = messages
        self._tasks = tasks
        self._exchanges = exchanges
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
            stores=_RunnerStores(
                messages=self._messages, tasks=self._tasks, exchanges=self._exchanges
            ),
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
        stranded = await self._reopen_stranded_exchanges()
        orphaned = await self._list_orphaned()
        for task in orphaned:
            await self._restart_orphaned(task)
        revived = await self._revive_unowned_open()
        undelivered = await self._list_undelivered()
        for task in undelivered:
            await self._redeliver_undelivered(task)
        logger.info(
            "startup recovery: reopened=%s restarted=%s revived=%s redelivered=%s",
            stranded,
            len(orphaned),
            revived,
            len(undelivered),
        )

    async def _revive_unowned_open(self) -> int:
        """Give every unowned OPEN exchange back to its dialog's sweep.

        Runs after the orphan restarts (which re-own their exchanges), so
        whatever is still unowned here is a genuine crash leftover: an
        exchange created whose run never came to exist.
        """
        try:
            stranded = await self._exchanges.list_unowned_open()
        except Exception:
            logger.exception("unowned-open startup sweep failed")
            return 0
        revived = 0
        for exchange in stranded:
            try:
                dialog = await self._dialogs.get(exchange.dialog_id)
                runner = await self.get_or_create_runner(dialog.user_id, dialog.channel)
                await runner.resume_stranded()
                revived += 1
            except Exception:
                logger.exception("stranded exchange revive failed: exchange=%s", exchange.id)
        return revived

    async def _reopen_stranded_exchanges(self) -> int:
        """Reset obligations whose owner died; return how many.

        Processes never survive a restart, so an IN_PROGRESS exchange at
        startup is stale by definition — and a settle that failed mid-write
        would otherwise strand it forever, invisible to the OPEN-based
        predicate. AWAITING_USER is left alone: that one waits for a human.
        """
        try:
            return await self._exchanges.reopen_in_progress()
        except Exception:  # recovery must never take the app down
            logger.exception("stranded exchange sweep failed")
            return 0

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

    async def evict(self, user_id: str, channel: str) -> None:
        """Stop and deregister the dialog's runner, if any (admin dialog deletion).

        The build memo and the live runner both go, so the next contact
        rebuilds from the (freshly emptied) persisted state instead of
        reusing an actor whose narrative outlived its rows. A dialog with
        nothing live is a no-op.
        """
        async with self._lock:
            build = self._builds.pop((user_id, channel), None)
        if build is None:
            return
        if not build.done():
            build.cancel()
        runner = None
        with suppress(asyncio.CancelledError, Exception):
            runner = await build
        if runner is None:
            return
        self._runners.pop(runner.dialog_id, None)
        try:
            await runner.stop()
        except Exception:  # a failing runner must not block the deletion
            logger.exception("runner stop failed: dialog=%s", runner.dialog_id)

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
