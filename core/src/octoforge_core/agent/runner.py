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
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

from octoforge_core.agent.branch import render_branch
from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    ProcessCompleted,
    ProcessStarted,
    ReasoningDelta,
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
from octoforge_core.cron.api import WakeOutcome
from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import (
    ClaimRepository,
    DialogClaim,
    DialogClaimList,
    DialogRepository,
    Exchange,
    ExchangeList,
    ExchangeNotFoundError,
    ExchangeRepository,
    ExchangeStatus,
    MessageRepository,
)
from octoforge_core.domain import (
    Attachment,
    AttachmentKind,
    ChatMessage,
    Dialog,
    MessageKind,
    MessageRole,
    MessageSource,
)
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.llm.usage import Usage
from octoforge_core.tariffs.api import LimitGate, UsageEvent, UsageKind, UsageOrigin
from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.store import TaskList, TaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import TaskDeleteOutcome, TaskDeleter, TaskSpawner, ToolContext
from octoforge_core.vision.api import ImageResolver, VisionClient, VisionUnavailableError

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 100

#: End-of-stream marker put on every subscriber queue when the actor stands
#: down. A transport that sees it must close its stream and reconnect rather
#: than keep waiting: this runner no longer owns the dialog, so nothing more
#: will ever arrive on this queue. Reconnecting is what lands the client on
#: whichever process took over.
STREAM_CLOSED = None

#: What a subscriber receives: events, then `STREAM_CLOSED` at most once.
SubscriberQueue = asyncio.Queue["ConversationEvent | None"]

#: How often a process refreshes the claims of its live dialogs. Also the
#: worst-case delay before an actor whose dialog moved stops streaming to a
#: transport — the per-run check in `_start_answer` covers new work sooner.
CLAIM_HEARTBEAT_SECONDS = 5.0

#: A claim unrefreshed for this long is treated as abandoned, so recovery may
#: take the dialog's stranded work. Several heartbeats of slack on purpose:
#: mistaking a slow query or a paused process for a dead one would hand a
#: live conversation to a second actor.
CLAIM_STALE_AFTER_SECONDS = 30.0

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
# how many pictures of one message the strong vision tier is shown at once
# (an album is one message): enough for a multi-page document, bounded
# because that tier is slow and paid per image
MAX_LOOK_IMAGES = 6
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
# how long a collection of forwarded material must stay quiet before the
# agent reacts to it on its own (no question ever came)
MATERIAL_QUIET_SECONDS = 30.0
MATERIAL_TITLE_TEMPLATE = "Переслано от {origin}"
MATERIAL_TITLE_ANONYMOUS = "Пересланные сообщения"
MATERIAL_TITLE_IMAGES = "Присланные изображения"
# the routing decision sees a preview, not the whole batch: the narrative
# keeps everything, the prompt stays bounded
MATERIAL_DIGEST_MESSAGES = 20
# The budget is the batch's, not each message's. Splitting it per message
# instead cost a routing decision in production (31.07): one forwarded post
# got the same 200 characters as one of twenty, and 200 characters of a
# forwarded picture are the image description, never the text under it.
MATERIAL_DIGEST_CHARS = 4000
# an over-budget piece is cut in the middle: a forwarded post carries its
# attribution and any picture description at the front and its own text at
# the end, so cutting only the tail drops exactly what identifies the topic
MATERIAL_DIGEST_ELLIPSIS = "\n[…]\n"
MATERIAL_DIGEST_TEMPLATE = (
    "The user forwarded {count} message(s) — third-party content, not their "
    "own words. Which exchange does this material belong to?\n{lines}"
)


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


def _untitled(message: ChatMessage) -> str:
    """Name a collection nobody attributed: say what it actually holds.

    A picture the user sent themselves is material like a forward, but
    calling its exchange "forwarded messages" would be a lie to the operator.
    """
    if any(item.kind is AttachmentKind.IMAGE for item in message.attachments):
        return MATERIAL_TITLE_IMAGES
    return MATERIAL_TITLE_ANONYMOUS


def _bounded_preview(pieces: Sequence[str], budget: int) -> str:
    """Join `pieces` into at most `budget` characters, sharing it equally.

    The budget belongs to the batch: one forward may spend all of it, twenty
    get a slice each. A piece over its share keeps both ends — the front
    carries attribution and any picture description, the back carries the
    text the user actually forwarded.
    """
    if not pieces:
        return ""
    share = max(budget // len(pieces), len(MATERIAL_DIGEST_ELLIPSIS) + 2)
    return "\n".join(_middle_out(piece, share) for piece in pieces)


def _middle_out(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, dropping the middle rather than the tail."""
    if len(text) <= limit:
        return text
    keep = limit - len(MATERIAL_DIGEST_ELLIPSIS)
    head = keep // 2
    return text[:head] + MATERIAL_DIGEST_ELLIPSIS + text[len(text) - (keep - head) :]


def _silent_done(task: Task) -> bool:
    """Whether the task finished with a deliberately empty result (nothing to show)."""
    return task.status is TaskStatus.DONE and not (task.result or "").strip()


def _muted_after_ask(event: LoopEvent) -> LoopEvent | None:
    """Silence what a run writes after `ask_user`; None means "drop the event".

    The question is already on its way to the user and the tool's ack says
    so ("end this run now without writing anything else"), but a run must
    still end with something — and a model that has nothing left to say
    tends to narrate the ack instead ("question delivered, waiting for your
    answer"), which reaches the user as a second, pointless message.

    Muting is deterministic, so it does not depend on the model obeying:
    text stops being broadcast and the final is emptied, which the existing
    empty-final path already treats as legitimate silence (nothing persisted,
    nothing delivered). Only the visible output is muted — the loop itself,
    its tool calls and the terminal event keep flowing.
    """
    if isinstance(event, (TextDelta, ReasoningDelta)):
        return None
    if isinstance(event, Finished):
        return replace(event, message=replace(event.message, content=""))
    return event


def _task_source_message(task: Task) -> str | None:
    """The narrative row id of the user message an ANSWER task answers."""
    raw = task.input.get("source_message_id")
    return raw if isinstance(raw, str) else None


def _task_origin(task: Task) -> UsageOrigin:
    """What started this task, for the usage ledger."""
    if task.kind is TaskKind.ANSWER:
        return UsageOrigin.INTERACTIVE
    if "cron_job_id" in task.input:
        return UsageOrigin.CRON
    return UsageOrigin.BACKGROUND


def _exchange_outcome(status: TaskStatus) -> ExchangeStatus:
    """How a run's terminal status settles the obligation it owed."""
    if status is TaskStatus.DONE:
        return ExchangeStatus.ANSWERED
    if status is TaskStatus.CANCELLED:
        return ExchangeStatus.CANCELLED
    return ExchangeStatus.FAILED


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


class _DialogImageInspector:
    """ImageInspector bound to one dialog: re-reads its most recent image.

    "The image" is the newest one in the narrative rather than an id the
    model has to quote: the follow-up question is virtually always about the
    picture just sent, and exposing internal refs to the LLM would invite it
    to invent them.
    """

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def look(self, question: str) -> str:
        return await self._runner.look_at_image(question)


@dataclass(frozen=True, slots=True)
class _RunnerStores:
    """Persistence collaborators of one dialog actor."""

    messages: MessageRepository
    tasks: TaskStore
    exchanges: ExchangeRepository
    claims: ClaimRepository
    uow: UnitOfWork


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


class _Unseen(StrEnum):
    """What a run missed: nothing, only forwarded material, or the user speaking.

    The distinction decides the settle: the user's own words reopen the
    exchange for a fresh answer, material only parks it as a collection so a
    burst gets one reaction instead of one per forward.
    """

    NONE = "none"
    MATERIAL_ONLY = "material_only"
    SPOKEN = "spoken"


@dataclass(frozen=True, slots=True)
class _PromoteCollected:
    """The sweep nominating a settled collection for promotion (re-checked)."""

    exchange_id: str


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
    # The row as the finishing write left it, when there was one. It saves a
    # read of what we just wrote. None on the recovery path (no run finished
    # here) and when finalization failed — the handler reads then, as before.
    task: Task | None = None
    # a message of the same exchange arrived after the run's last sync: the
    # answer could not have accounted for it, so the exchange reopens
    unseen_messages: _Unseen = _Unseen.NONE


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


_Command = _Submit | _ProcessTerminated | _Flush | _PromoteCollected


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
    # the run called `ask_user` and its question already went out: everything
    # it writes afterwards is muted (see `_muted_after_ask`)
    asked: bool = False
    # what started this run, for the usage ledger
    origin: UsageOrigin = UsageOrigin.INTERACTIVE
    # tokens of every iteration so far — the terminal event alone carries only
    # the last iteration's usage, which would undercount multi-tool runs
    spent_prompt: int = 0
    spent_completion: int = 0


def _observe_spend(process: _Process, event: LoopEvent) -> None:
    """Accumulate an iteration's token usage onto the run."""
    if isinstance(event, AssistantMessage) and event.usage is not None:
        process.spent_prompt += event.usage.prompt_tokens
        process.spent_completion += event.usage.completion_tokens


class DialogSurface(Protocol):
    """A transport that renders one dialog's events wherever its user is.

    The actor broadcasts; somebody has to be listening on the user's behalf,
    and for a chat that somebody must exist even when the user is not looking
    — a scheduled run finishing at four in the morning still has to arrive.
    Attaching is therefore tied to the actor's life, not to a request.

    Core calls this and knows nothing else about the transport. The
    composition root decides which surface, if any, a given channel gets.
    """

    async def attach(self, runner: "ConversationRunner") -> None:
        """Start rendering this dialog; called once its actor exists."""
        ...

    async def detach(self, runner: "ConversationRunner") -> None:
        """Stop rendering it; the actor is going away or has moved elsewhere."""
        ...


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
    # limit checks and the usage ledger; None = unlimited and unmetered
    limits: LimitGate | None = None
    # quiet window before collected material earns a reaction of its own
    material_quiet_seconds: float = MATERIAL_QUIET_SECONDS
    # the strong vision tier plus the surface that can fetch an attachment
    # back into bytes; either being None turns `image_look` off entirely
    vision: VisionClient | None = None
    image_resolver: ImageResolver | None = None


class ConversationRunner:
    """Actor owning a dialog's narrative, processes, deliveries and subscribers."""

    def __init__(
        self,
        dialog: Dialog,
        config: RunnerConfig,
        stores: _RunnerStores,
        history: list[ChatMessage],
        claim: DialogClaim,
    ) -> None:
        messages, tasks, exchanges = stores.messages, stores.tasks, stores.exchanges
        self._dialog = dialog
        self._claims = stores.claims
        self._uow = stores.uow
        # the claim this actor was born with: everything user-visible is
        # gated on it still being the current one
        self._claim = claim
        self._stood_down = False
        # set when the actor itself discovers the dialog moved: it stops
        # taking work and exits, and the manager's heartbeat finishes the
        # stand-down (closing subscribers) a moment later
        self._preempted = False
        self._loop = config.loop
        self._prompts = config.prompts
        self._router = config.router
        self._max_processes = config.max_processes
        self._material_quiet_seconds = config.material_quiet_seconds
        self._vision = config.vision
        self._image_resolver = config.image_resolver
        self._task_outcome_listener = config.task_outcome_listener
        self._limits = config.limits
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
        self._subscribers: set[SubscriberQueue] = set()
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
        source: MessageSource | None = None,
    ) -> None:
        """Submit a user message; the router decides which exchange it joins.

        `client_message_id` is an idempotency key: a repeat with an
        already-recorded key is skipped (delivery retries are normal).
        `reply_to_exchange_id` lets a transport that knows the user replied to
        a specific message name the exchange outright, skipping the router.
        `source` says what the message is: `MATERIAL` marks content the user
        shared rather than wrote (a forward), which never opens an obligation
        and never starts a run — its `origin` titles the exchange that
        collects it, and its `attachments` reference files the transport has
        already described in the text, so a tool can revisit them.
        """
        resolved = source or MessageSource()
        await self._inbox.put(
            _Submit(
                ChatMessage(
                    role=MessageRole.USER,
                    content=content,
                    kind=resolved.kind,
                    attachments=resolved.attachments,
                ),
                client_message_id=client_message_id,
                reply_to_exchange_id=reply_to_exchange_id,
                cancel_epoch=self._cancel_epoch,
                origin=resolved.origin,
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
        await self._tasks.mark_failed(task.id, RESTART_LIMIT_ERROR)
        self._pending_deliveries.append(
            _Delivery(events=(Failed(error=RESTART_LIMIT_ERROR),), task_id=task.id)
        )
        await self._flush_deliveries()

    async def _start_orphaned(self, task: Task) -> None:
        """Start the replacement background process of an orphaned task."""
        if task.kind is not TaskKind.ANSWER:
            self._start_process(task)
            return
        exchange_id = task.exchange_id
        if exchange_id is not None and await self._exchange_awaits_user(exchange_id):
            # the run died between ask_user and its finalization: the ask
            # already went out and the exchange waits for the user — a
            # restarted run would clobber that state and duplicate the work.
            # Close the row silently; the user's reply resumes the exchange.
            await self._tasks.mark_done(task.id, "")
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
                await self._exchanges.set_status(exchange_id, ExchangeStatus.IN_PROGRESS)

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
                # kept in the input too: it is part of what the run was given,
                # and a run's input is not rewritten after the fact
                task_input["exchange_id"] = source.exchange_id
        task = Task(
            dialog_id=self._dialog.id,
            title=title,
            kind=kind,
            # the obligation as a column: joinable, indexable, and checked by
            # the database, which a key inside a JSON blob is none of
            exchange_id=source.exchange_id if source is not None else None,
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

    def subscribe(self) -> SubscriberQueue:
        """Attach a subscriber queue receiving broadcast events.

        Attaching also asks the actor to drain the outbox: results that
        terminated while no transport was attached wait there (a cron firing
        into a dialog nobody watches, the startup redelivery sweep, which runs
        before the surfaces come up). Live stream events are never replayed —
        only the outbox is.

        A runner that has already stood down hands back an
        already-closed queue instead of a silent one: the transport learns
        immediately that it must go and find the current owner.
        """
        queue: SubscriberQueue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        if self._stood_down:
            queue.put_nowait(STREAM_CLOSED)
            return queue
        self._subscribers.add(queue)
        self._inbox.put_nowait(_Flush())
        return queue

    def unsubscribe(self, queue: SubscriberQueue) -> None:
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

    @property
    def user_id(self) -> str:
        """The user this dialog belongs to."""
        return self._dialog.user_id

    @property
    def channel(self) -> str:
        """The surface this dialog belongs to."""
        return self._dialog.channel

    @property
    def claim(self) -> DialogClaim:
        """The claim this actor holds on its dialog."""
        return self._claim

    async def stand_down(self) -> None:
        """Stop for good: another process owns this dialog now.

        Not a failure and not a cancellation by the user — the work simply
        moved. Every subscriber is told the stream is over so it reconnects
        and finds the new owner; whatever this actor had in flight is left
        for that owner's recovery, which is the same path a crash takes.

        Called by the manager (which owns the claim lifecycle), never from
        inside the actor: `stop()` awaits the actor task, and a task cannot
        await itself. An actor that notices the loss on its own instead
        refuses the work and exits — see `_start_answer`.

        Idempotent: preemption can be noticed by the heartbeat and by a run
        starting at the same time.
        """
        if self._stood_down:
            return
        self._stood_down = True
        logger.info(
            "standing down: dialog=%s owner=%s generation=%s",
            self._dialog.id,
            self._claim.owner,
            self._claim.generation,
        )
        for queue in tuple(self._subscribers):
            self._close_stream(queue)
        self._subscribers.clear()
        await self.stop()

    @staticmethod
    def _close_stream(queue: SubscriberQueue) -> None:
        """Put the end-of-stream marker, evicting a queued event if need be.

        A full queue must not swallow the marker: a transport that never
        learns the stream is over waits on a runner that will never speak
        again, which looks exactly like the agent ignoring the user.
        """
        while True:
            try:
                queue.put_nowait(STREAM_CLOSED)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # drained concurrently; try the put again
                    continue

    async def _still_owns_dialog(self) -> bool:
        """Whether this actor's claim is still the current one.

        Deliberately fails OPEN. A database hiccup must not stop the single
        process installation — the overwhelmingly common one — from
        answering, and the heartbeat is the mechanism that ultimately
        notices a lost dialog. This check only narrows the window between a
        handover and the next heartbeat.
        """
        try:
            generation = await self._claims.current_generation(self._dialog.id)
        except Exception:
            logger.exception("ownership check failed: dialog=%s", self._dialog.id)
            return True
        return generation is None or generation == self._claim.generation

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
            if self._preempted:
                # another process owns this dialog now: take no further
                # commands. The manager's heartbeat completes the stand-down
                # (subscribers get the end-of-stream marker) right after.
                raise asyncio.CancelledError from None
            if self._cancellation_pending():
                # A cancel can also be absorbed WITHOUT surfacing as an error:
                # SQLAlchemy's greenlet bridge swallows the CancelledError of
                # a store call in flight and lets the await return normally.
                # The dispatch then finishes, the loop parks on the inbox
                # again, and `stop()` waits on a task that will never end —
                # a deadlock that hangs app shutdown and admin dialog
                # deletion. Measured on Python 3.11 (what the container and CI
                # run); 3.12 schedules just differently enough to hide it.
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
            await self._flush_deliveries()
        elif isinstance(command, _PromoteCollected):
            await self._handle_promote(command)

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
        await self._record_user_message()
        if message.kind is MessageKind.MATERIAL:
            await self._collect_material(message, command)
            return
        # one read serves both the routing decision and the nudge after it
        live = await self._exchanges.list_live(self._dialog.id)
        decision = await self._route(message, command, live)
        await self._record_routing(decision.usage)
        await self._apply_route(message, decision, command, live)

    async def _record_user_message(self) -> None:
        """Ledger one persisted user message (duplicates never get here)."""
        if self._limits is None:
            return
        await self._limits.record(
            UsageEvent(
                user_id=self._dialog.user_id,
                kind=UsageKind.USER_MESSAGE,
                origin=UsageOrigin.INTERACTIVE,
                quantity=1,
                dialog_id=self._dialog.id,
            )
        )

    async def _record_routing(self, usage: Usage | None) -> None:
        """Ledger a routing LLM call; the deterministic paths carry no usage."""
        if self._limits is None or usage is None:
            return
        await self._limits.record(
            UsageEvent(
                user_id=self._dialog.user_id,
                kind=UsageKind.LLM_ROUTING,
                origin=UsageOrigin.INTERACTIVE,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                dialog_id=self._dialog.id,
            )
        )

    async def _collect_material(self, message: ChatMessage, command: _Submit) -> None:
        """Take in forwarded material: it owes nothing, so nothing starts.

        Material is someone else's text the user shared. It joins the
        dialog's single collecting exchange (created on the first forward),
        which is durable state, not a buffer: a restart mid-burst loses
        nothing and the sweep still reacts. The touch is the quiet clock —
        a burst that keeps arriving keeps postponing the reaction.
        """
        exchange = await self._material_home(message, command.origin)
        message = replace(message, exchange_id=exchange.id)
        self._narrative.append(message)
        if message.id is not None:
            await self._messages.set_exchange(message.id, exchange.id)
        await self._exchanges.touch(exchange.id)
        logger.info(
            "material collected: dialog=%s exchange=%s origin=%s",
            self._dialog.id,
            exchange.id,
            command.origin,
        )

    async def _material_home(self, message: ChatMessage, origin: str | None) -> Exchange:
        """Where this forward belongs: a question being answered, or a collection.

        The overwhelmingly common shape is "comment, then forwards" — the
        client sends the user's own line first, so a run is already going by
        the time the material lands. That run is what the material is for, so
        it joins its exchange and the pull model syncs it in; if the run
        already finished, the unseen-material rule parks the exchange and the
        sweep reacts once more with the full picture. Waiting for a router
        decision here would answer the question before the material arrived
        (measured live, 29.07).
        """
        answering = [
            item
            for item in await self._exchanges.list_live(self._dialog.id)
            if self._live_process_for(item.id) is not None
        ]
        if answering:
            return max(answering, key=lambda item: item.updated_at)
        return await self._collecting_exchange(message, origin)

    async def _collecting_exchange(self, message: ChatMessage, origin: str | None) -> Exchange:
        """The dialog's collecting exchange, created on the first forward.

        One per dialog: a burst is one reaction, not one per message. The
        title comes from the first forward's origin — it stays as the honest
        description of where the material came from even after a question
        joins the exchange.
        """
        existing = await self._exchanges.find_collecting(self._dialog.id)
        if existing is not None:
            return existing
        title = MATERIAL_TITLE_TEMPLATE.format(origin=origin) if origin else _untitled(message)
        return await self._exchanges.create(
            self._dialog.id, title, status=ExchangeStatus.COLLECTING
        )

    async def promote_collected(self, exchange_id: str) -> None:
        """Turn a settled collection into an obligation (sweep entry point).

        Routed through the inbox: the decision must be serialized with
        routing and run starts, or a promotion could race a question that is
        already starting a run on the same exchange.
        """
        await self._inbox.put(_PromoteCollected(exchange_id=exchange_id))

    async def _handle_promote(self, command: _PromoteCollected) -> None:
        """Promote a collection whose quiet window elapsed; re-checked here.

        The sweep only nominates: by the time the actor gets here a question
        may have adopted the collection (it is no longer COLLECTING) or fresh
        material may have restarted the clock. Both mean "not now".
        """
        try:
            exchange = await self._exchanges.get(command.exchange_id)
        except ExchangeNotFoundError:
            return
        if exchange.status is not ExchangeStatus.COLLECTING:
            return
        if (utc_now() - exchange.updated_at).total_seconds() < self._material_quiet_seconds:
            return  # material arrived after the sweep picked it: let it settle
        logger.info(
            "promoting collected material: dialog=%s exchange=%s",
            self._dialog.id,
            exchange.id,
        )
        target = await self._material_target(exchange)
        if target is not None:
            await self._reparent_material(exchange, target)
            return
        await self._exchanges.set_status(exchange.id, ExchangeStatus.OPEN)
        await self._resume_open_exchange(exchange.id, notify_limit=False)

    async def _material_target(self, collection: Exchange) -> Exchange | None:
        """Ask the router whether the batch belongs to another live exchange.

        Only asked when there is something to belong to, and only once per
        batch — the decision rides the quiet window, so its latency is free.
        The material itself is untrusted third-party text, so the decision is
        narrowed to "which exchange": `cancel_ids` are dropped and COMMAND is
        read as "no target". A forwarded "stop everything" must stay text.
        """
        live = [
            item
            for item in await self._exchanges.list_live(self._dialog.id)
            if item.id != collection.id and item.status is not ExchangeStatus.COLLECTING
        ]
        if not live:
            return None
        infos = tuple(
            ExchangeInfo(
                id=item.id,
                title=item.title,
                status=item.status,
                pending_question=item.pending_question,
                age_seconds=(utc_now() - item.updated_at).total_seconds(),
            )
            for item in live
        )
        decision = await self._router.route(
            infos, self._material_digest(collection.id), self._max_processes
        )
        await self._record_routing(decision.usage)
        if decision.action is not RouteAction.CONTINUE or decision.exchange_id is None:
            return None
        return next((item for item in live if item.id == decision.exchange_id), None)

    def _material_digest(self, exchange_id: str) -> str:
        """The batch as the question a promotion asks the router."""
        pieces = self._material_pieces(exchange_id)
        return MATERIAL_DIGEST_TEMPLATE.format(
            count=len(pieces), lines=_bounded_preview(pieces, MATERIAL_DIGEST_CHARS)
        )

    def _material_preview(self, exchange_id: str) -> str | None:
        """What a collection holds, for the router's candidate line.

        A collection is named after where the forward came from, so its title
        cannot answer "is this message about it?" — the content has to. None
        when there is nothing collected (the caller then shows the title
        alone).
        """
        pieces = self._material_pieces(exchange_id)
        return _bounded_preview(pieces, MATERIAL_DIGEST_CHARS) if pieces else None

    def _material_pieces(self, exchange_id: str) -> list[str]:
        """The collected material of one exchange, oldest first, capped in count."""
        return [
            message.content
            for message in self._narrative
            if message.exchange_id == exchange_id and message.kind is MessageKind.MATERIAL
        ][:MATERIAL_DIGEST_MESSAGES]

    async def _reparent_material(self, collection: Exchange, target: Exchange) -> None:
        """Move the batch into the exchange it belongs to and drop the shell.

        The target gets the material as clarifying context: a live run pulls
        it in at the next sync, a finished one is reopened by the
        unseen-messages rule, and a parked one resumes.
        """
        logger.info(
            "material reparented: dialog=%s from=%s to=%s",
            self._dialog.id,
            collection.id,
            target.id,
        )
        for index, message in enumerate(self._narrative):
            if message.exchange_id != collection.id:
                continue
            self._narrative[index] = replace(message, exchange_id=target.id)
            if message.id is not None:
                await self._messages.set_exchange(message.id, target.id)
        await self._exchanges.set_status(collection.id, ExchangeStatus.CANCELLED)
        await self._exchanges.touch(target.id)
        if self._live_process_for(target.id) is not None:
            return  # a live run pulls the material in at its next sync
        await self._exchanges.set_status(target.id, ExchangeStatus.OPEN)
        await self._resume_open_exchange(target.id, notify_limit=False)

    async def _route(
        self, message: ChatMessage, command: _Submit, live: ExchangeList
    ) -> RouteDecision:
        """Decide which exchange the message belongs to (deterministic first).

        Three shortcuts skip the LLM entirely: an explicit transport-level
        reply names its exchange outright, with no live exchange there is
        nothing to belong to, and a message arriving on the heels of a
        forward is that forward's ("forward, then ask" — see
        `_sole_fresh_collection`). Everything else goes to the router, which
        sees exchanges (user-visible obligations), not process ids.

        `live` comes from the caller: the same read serves this decision and
        the nudge that follows it.
        """
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
        collection = self._sole_fresh_collection(live)
        if collection is not None:
            logger.info(
                "routed by collection: dialog=%s exchange=%s",
                self._dialog.id,
                collection.id,
            )
            return RouteDecision(action=RouteAction.CONTINUE, exchange_id=collection.id)
        infos = tuple(
            ExchangeInfo(
                id=item.id,
                title=item.title,
                status=item.status,
                pending_question=item.pending_question,
                # staleness, not lifetime: "how long since we last touched
                # it" is what a routing decision needs (matches the nudge)
                age_seconds=(utc_now() - item.updated_at).total_seconds(),
                # a collection's title names the forward's source, not its
                # subject: without the content the router cannot tell whether
                # the message is about it (measured live, 31.07)
                preview=(
                    self._material_preview(item.id)
                    if item.status is ExchangeStatus.COLLECTING
                    else None
                ),
            )
            for item in live
        )
        return await self._router.route(infos, message.content, self._max_processes)

    def _sole_fresh_collection(self, live: ExchangeList) -> Exchange | None:
        """The collection a message arriving right now simply belongs to.

        "Forward, then ask" is the mirror of the shape `_material_home`
        already handles without a router: there the comment comes first and
        the material joins its run, here the material comes first and the
        message adopts it. Waiting for an LLM to notice that costs the answer
        — the router sees titles, and a collection is titled after the
        forward's source (measured live, 31.07: the question opened its own
        exchange and the batch was promoted separately, so the user got asked
        what to do with a post they had already asked about).

        Narrow on purpose. Only when the collection is the *only* live
        exchange is "this belongs to the forward" the sole reading; with
        another obligation in flight the message may be a reply to that one,
        and the router decides — now with the collection's content in hand.
        Only while the batch is still fresh: past the quiet window the sweep
        owns it, and jumping in would race the promotion this serializes with.
        Being wrong here costs a forward as extra context, not a lost answer.
        """
        if len(live) != 1:
            return None
        collection = live[0]
        if collection.status is not ExchangeStatus.COLLECTING:
            return None
        if (utc_now() - collection.updated_at).total_seconds() >= self._material_quiet_seconds:
            return None
        return collection

    async def ask_user(self, process_id: str, question: str) -> bool:
        """Deliver a run's clarifying question and park its exchange.

        Called from the `ask_user` tool inside a pump task. The obligation is
        NOT closed: it moves to AWAITING_USER, so the "what is left to do"
        predicate skips it (that is the user's move) while it stays visible
        to the agent, the reminder and the operator console. Returns whether
        the question was actually delivered: a RUN/cron process has no
        exchange to park, so asking is unavailable there — the tool must
        report that honestly instead of promising a reply.

        Delivering also mutes the rest of the run (`_muted_after_ask`): the
        user has been asked, so nothing this run writes afterwards is an
        answer to anything.
        """
        process = self._processes.get(process_id)
        if process is None or process.exchange_id is None:
            return False
        process.asked = True
        await self._deliver_notice(question, exchange_id=process.exchange_id)
        with suppress(ExchangeNotFoundError):
            # keep ownership: the asking run is still alive (it unwinds on
            # its own schedule after the ack) — clearing the owner here let
            # a prompt reply spawn a second run on the same exchange, both
            # streaming into one transport draft
            await self._exchanges.set_status(
                process.exchange_id,
                ExchangeStatus.AWAITING_USER,
                pending_question=question,
            )
        return True

    @property
    def _can_see_images(self) -> bool:
        """Whether this dialog has both a vision model and a way to fetch files."""
        return self._vision is not None and self._image_resolver is not None

    async def look_at_image(self, question: str) -> str:
        """Ask the strong vision model about the dialog's most recent picture(s).

        The cheap description written at ingestion answers most questions;
        this is the escape hatch for the ones it cannot, so it is spent only
        when a tool call asked for it.

        "Most recent" means one message, not one file: an album arrives as a
        single message carrying every picture (the three pages of one menu),
        and answering about page one alone would repeat the very blindness
        the tool exists to cure. The batch is capped at `MAX_LOOK_IMAGES` —
        the strong tier is slow and paid per image.
        """
        if self._vision is None or self._image_resolver is None:
            raise VisionUnavailableError("vision is not configured")
        attachments = self._latest_images()
        if not attachments:
            raise VisionUnavailableError("no image in this dialog")
        logger.info(
            "looking at images again: dialog=%s refs=%s",
            self._dialog.id,
            [item.ref for item in attachments],
        )
        # concurrently: an album can carry several images, and fetching them one
        # after another made the user wait for the sum of the round trips while
        # the actor was blocked on each in turn
        images = tuple(
            await asyncio.gather(
                *(self._image_resolver.fetch(attachment.ref) for attachment in attachments)
            )
        )
        return await self._vision.look(images, question)

    def _latest_images(self) -> tuple[Attachment, ...]:
        """Every image of the newest narrative message that carries one."""
        for message in reversed(self._narrative):
            images = tuple(
                item for item in message.attachments if item.kind is AttachmentKind.IMAGE
            )
            if images:
                return images[:MAX_LOOK_IMAGES]
        return ()
        return None

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
            parked = (
                exchange.status is ExchangeStatus.AWAITING_USER
                and self._live_process_for(exchange.id) is None
            )
            # a stop also drops material waiting for a reaction: the user
            # said "never mind", so the collection must not fire later
            if parked or exchange.status is ExchangeStatus.COLLECTING:
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
        task = command.task
        if task is None:
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

        One guarded write instead of a read followed by a write. The guard is
        that this run still owns the exchange: it may have changed hands while
        the termination sat in the actor's inbox (a follow-up spawned a fresh
        owner, or a cancel cleared it), and settling it then would clobber a
        live run or resurrect a cancelled exchange. As a preceding SELECT that
        check had a window after it; in the WHERE clause it has none.

        Which state to move to is decided from the command alone:

        - something arrived after the run's last sync — the user's own words
          reopen the exchange (even one it had asked about), while material
          only parks it as a collection, so a forward burst reacts once when
          the batch settles rather than once per message;
        - otherwise the run's own outcome, except that an exchange parked on
          the user's answer stays parked (`keep_if_awaiting`) — the question
          went out, the next move is theirs. A user cancel still closes it,
          or the nudge would re-ask what they explicitly stopped.
        """
        if command.exchange_id is None or command.exchange_status is None:
            return
        cancelled = command.exchange_status is ExchangeStatus.CANCELLED
        if not cancelled and command.unseen_messages is _Unseen.SPOKEN:
            if await self._settle_to(command, ExchangeStatus.OPEN):
                await self._resume_open_exchange(command.exchange_id)
            return
        if not cancelled and command.unseen_messages is _Unseen.MATERIAL_ONLY:
            if await self._settle_to(command, ExchangeStatus.COLLECTING):
                await self._exchanges.touch(command.exchange_id)
            return
        await self._settle_to(command, command.exchange_status, keep_if_awaiting=not cancelled)

    async def _settle_to(
        self,
        command: _ProcessTerminated,
        status: ExchangeStatus,
        *,
        keep_if_awaiting: bool = False,
    ) -> bool:
        """Write the settled status under the ownership guard; False when it did not apply."""
        assert command.exchange_id is not None  # the caller checked
        settled = await self._exchanges.settle_owned(
            command.exchange_id,
            command.task_id,
            status,
            keep_if_awaiting=keep_if_awaiting,
        )
        logger.info(
            "settling exchange: dialog=%s exchange=%s to=%s unseen=%s applied=%s",
            self._dialog.id,
            command.exchange_id,
            status.value,
            command.unseen_messages,
            settled is not None,
        )
        return settled is not None

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
                if (
                    exchange.status is not ExchangeStatus.OPEN
                    or self._live_process_for(exchange_id) is not None
                ):
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
                    exchange_id=task.exchange_id,
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
                    exchange_id=task.exchange_id,
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
                    exchange_id=task.exchange_id,
                )
            )
        else:
            self._pending_deliveries.append(
                _Delivery(
                    events=(_delivery_started(task), Failed(error=terminal.error)),
                    task_id=task.id,
                    exchange_id=task.exchange_id,
                )
            )

    async def _mark_streamed_delivered(self, task: Task) -> None:
        """Stamp a task as delivered: the user already watched it stream live.

        A no-op when the finishing write already stamped it — which is the
        common case now (`_delivery_is_certain`), and the reason this costs
        no statement on the live-answer path.
        """
        if task.delivered_at is not None:
            return
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
        self,
        message: ChatMessage,
        decision: RouteDecision,
        command: _Submit,
        live: ExchangeList,
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
        created: Exchange | None = None
        refused = False
        if decision.action is RouteAction.CONTINUE and decision.exchange_id is not None:
            exchange_id = decision.exchange_id
            # before the owner: the run about to start titles its task from
            # the exchange, and it should carry the name that now describes it
            await self._retitle(exchange_id, decision.title)
        elif decision.action is not RouteAction.COMMAND:
            if self._exceeds_limit(self._cancelled_tasks(cancelled)):
                refused = True
            else:
                created = await self._exchanges.create(self._dialog.id, message.content)
                exchange_id = created.id
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
                known=created,
            )
        await self._nudge_stale_exchanges(exchange_id, live, cancelled)

    async def _retitle(self, exchange_id: str, title: str | None) -> None:
        """Rename an exchange a message just joined; never fatal.

        An exchange is named when it opens, after the message that opened it,
        and a few turns later that name describes a sentence rather than the
        obligation — a collection's name is its source, which never described
        the subject at all. Routing renames it as it attaches, so the operator
        console, the nudge and the next routing decision all see what the
        exchange is actually about.

        The name is cosmetic and the answer is not: a store failure here must
        not strand the message the way it would if it propagated (the same
        reason the nudge sits after owner assignment).
        """
        if title is None:
            return
        try:
            await self._exchanges.set_title(exchange_id, title)
        except Exception:  # a name is never worth losing the answer over
            logger.warning(
                "retitle failed: dialog=%s exchange=%s", self._dialog.id, exchange_id, exc_info=True
            )

    async def _cancel_exchanges(self, exchange_ids: tuple[str, ...]) -> set[str]:
        """Cancel the named exchanges and their live runs; return what was cancelled.

        The exchange is no longer read first: it was read for its owner, and
        the run answering it is something this process knows without asking.
        A missing row still surfaces — from the write, which raises the same
        error the read did.
        """
        cancelled: set[str] = set()
        for exchange_id in exchange_ids:
            with suppress(ExchangeNotFoundError):
                answering = self._live_process_for(exchange_id)
                if answering is not None:
                    self._cancel_process(answering.task_id)
                await self._exchanges.set_status(exchange_id, ExchangeStatus.CANCELLED)
                cancelled.add(exchange_id)
        return cancelled

    async def _ensure_owner(  # noqa: PLR0913, PLR0917 — one call site, all of it needed
        self,
        exchange_id: str,
        message: ChatMessage,
        client_key: str | None,
        cancelled: set[str] | None = None,
        cancel_epoch: int | None = None,
        known: Exchange | None = None,
    ) -> None:
        """Make sure a live run owes this exchange an answer.

        An exchange already being answered needs nothing: its run pulls the
        new message in at the next iteration sync (the pull model). One that
        is open or was waiting for the user gets a run now. `cancelled` are
        the exchanges the same routing decision just cancelled: their runs
        unwind cooperatively, so the limit check must discount them or a
        "stop X, continue Y" message hits the limit on X's still-live slot.

        `known` is the exchange this very turn just created. Only then may the
        read be skipped: its id has not left this coroutine, so nothing can
        have claimed it in between. For an exchange that already existed the
        read stays — and stays inside the lock.
        """
        with suppress(ExchangeNotFoundError):
            async with self._spawn_lock:
                # read the exchange INSIDE the lock: a check-then-act across
                # the lock await is how a second owner slips in
                if self._live_process_for(exchange_id) is not None:
                    return
                exchange = known if known is not None else await self._exchanges.get(exchange_id)
                if self._exceeds_limit(self._cancelled_tasks(cancelled or set())):
                    await self._reject_for_limit(message)
                    return
                await self._start_answer(exchange, message, client_key, cancel_epoch=cancel_epoch)

    async def _nudge_stale_exchanges(
        self,
        current_exchange_id: str | None,
        live: ExchangeList,
        cancelled: set[str],
    ) -> None:
        """Remind the user about a question they left hanging (event-driven).

        Fires when a new message arrives while some other exchange has been
        waiting for a reply longer than `NUDGE_AFTER_SECONDS` — as its own
        message, quoting the agent's own pending question.

        `live` is this intake's routing read, not a fresh one: between the
        two moments the set changes only through this coroutine's own hands
        (`cancelled` and the current exchange, both excluded here), and a
        cosmetic reminder does not get a round trip of its own. A run
        settling an AWAITING_USER exchange concurrently could slip a stale
        reminder through — exactly as it could with a fresh read a
        millisecond older.
        """
        now = utc_now()
        for exchange in live:
            if (
                exchange.id == current_exchange_id
                or exchange.id in cancelled
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

    def _live_process_for(self, exchange_id: str | None) -> _Process | None:
        """The run this actor has going for the exchange, if any.

        Asked of memory, not of a column. This process holds the dialog's
        claim, so a run of one of its exchanges is a run here — the stored
        owner was only ever used as a key into this very dict.
        """
        if exchange_id is None:
            return None
        return next(
            (process for process in self._processes.values() if process.exchange_id == exchange_id),
            None,
        )

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

        Ownership is checked here, once per run, and nowhere on the streaming
        path: one query is nothing beside a model call, while a per-event
        check would put a database round trip on the hot loop. Losing the
        dialog here leaves the exchange OPEN and untouched — exactly the
        state the new owner's recovery picks up, the same shape a crash
        leaves behind.
        """
        if not await self._still_owns_dialog():
            self._preempted = True
            logger.info(
                "refusing to answer, dialog moved: dialog=%s exchange=%s",
                self._dialog.id,
                exchange.id,
            )
            return
        # the task and the IN_PROGRESS flip land together: a crash between
        # them used to leave an IN_PROGRESS exchange with no task behind it —
        # a state only the reopen sweep could untangle
        async with self._uow():
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
            await self._exchanges.set_status(exchange.id, ExchangeStatus.IN_PROGRESS)
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
            # the live-exchange read rides beside the assemble: it does not
            # touch the narrative the lock protects, and marking uses durable
            # state as of the read either way — an exchange settling during
            # the assemble was always a race, with the read after it or beside
            assembled, live_ids = await asyncio.gather(
                self._compactor.assemble(self._dialog, self._narrative),
                self._live_exchange_ids(own_exchange_id),
            )
            self._trim_narrative(assembled.snapshot_len - assembled.tail_count)
        narrative = render_branch(assembled.messages, own_exchange_id, live_ids)
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
            exchange_id=task.exchange_id,
            origin=_task_origin(task),
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
        # None when finalization failed: the handler falls back to reading the
        # row, exactly as it did before it was handed one
        task: Task | None = None
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
                status, task = await self._finalize(process, terminal)
            except Exception:  # a store failure must not wedge the process slot
                logger.exception(
                    "process finalize failed: dialog=%s process=%s", self._dialog.id, process.id
                )
        finally:
            self._terminate_process(process, status, terminal, task)

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

    def _outgoing(self, process: _Process, event: LoopEvent) -> LoopEvent | None:
        """The event as subscribers should see it; None when it is not shown at all."""
        out = event
        if isinstance(event, Finished):
            # reply threading: the loop knows nothing about tasks
            out = replace(event, source_client_message_id=process.source_client_message_id)
        if process.asked:
            return _muted_after_ask(out)
        return out

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
            image_inspector=_DialogImageInspector(self) if self._can_see_images else None,
            owner_task_id=process.task_id,
        )
        terminal: LoopEvent = Failed(error="loop ended without a terminal event")
        try:
            async for event in self._loop.stream(process.branch, process.control, context):
                if isinstance(event, IterationStarted):
                    # pull model: re-sync the narrative part of the branch
                    # before the loop's next LLM call reads it
                    await self._sync_branch(process)
                _observe_spend(process, event)
                out = self._outgoing(process, event)
                if out is None:
                    continue
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
        self,
        process: _Process,
        status: TaskStatus,
        terminal: LoopEvent,
        task: Task | None = None,
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
                unseen_messages=self._unseen_kind(process),
                task=task,
            )
        )

    def _unseen_kind(self, process: _Process) -> _Unseen:
        """What arrived for this run's exchange after its last sync.

        Replaces the requeue heuristic: instead of re-submitting messages, the
        exchange goes back to OPEN and gets a fresh run — the durable state
        decides, not an in-memory scan. Material is distinguished from the
        user's own words: a forward burst landing in a live exchange would
        otherwise reopen it once per message, which is the very "one answer
        per forward" bug this whole model exists to remove. Material instead
        parks the exchange as a collection and reacts once, when it settles.
        """
        if not process.narrative_built or process.exchange_id is None:
            return _Unseen.NONE
        arrived = [
            message
            for message in self._narrative[process.watermark :]
            if message.role is MessageRole.USER and message.exchange_id == process.exchange_id
        ]
        if not arrived:
            return _Unseen.NONE
        if all(message.kind is MessageKind.MATERIAL for message in arrived):
            return _Unseen.MATERIAL_ONLY
        return _Unseen.SPOKEN

    async def _finalize(self, process: _Process, terminal: LoopEvent) -> tuple[TaskStatus, Task]:
        """Fold the run outcome into the narrative and the task store.

        Returns the status and the task as the store now holds it. The row
        used to be fetched here before writing it and fetched again by the
        delivery handler afterwards; nothing was ever decided on the first
        read, and the second one is the same row this write produced.

        An empty final is a process choosing silence (e.g. its question was
        taken over by another process, or the user said to drop it): the task
        completes, but no empty bubble enters the narrative and nothing is
        delivered — an undelivered empty result would otherwise be redelivered
        by the startup sweep forever (see _handle_terminated's silent-done stamp).
        """
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
            delivered = self._delivery_is_certain(process, terminal.message.content)
            if terminal.message.content.strip():
                message = replace(
                    terminal.message,
                    task_id=process.task_id,
                    exchange_id=process.exchange_id,
                )
                # the answer and the task's terminal state land together or
                # not at all: a crash between them used to leave an answered
                # narrative under a still-RUNNING task, which recovery would
                # then answer again
                async with self._uow():
                    await self._persist(message, usage=terminal.usage)
                    task = await self._tasks.mark_done(
                        process.task_id, terminal.message.content, delivered=delivered
                    )
                # in-memory state only after the commit: a rolled-back unit
                # must not leave a phantom message in the narrative
                self._narrative.append(message)
                if not process.narrative_built:
                    # RUN/cron results grow the narrative too, but only
                    # answer runs assemble branches — a dialog fed purely by
                    # cron would never trigger compaction and grow unbounded
                    await self._compact_after_run_final()
            else:
                task = await self._tasks.mark_done(
                    process.task_id, terminal.message.content, delivered=delivered
                )
            status = TaskStatus.DONE
        elif isinstance(terminal, Failed):
            task = await self._tasks.mark_failed(
                process.task_id,
                terminal.error,
                delivered=self._delivery_is_certain(process, terminal.error),
            )
            status = TaskStatus.FAILED
        else:
            async with self._uow():
                salvaged = await self._salvage_interrupted_turn(process)
                task = await self._tasks.mark_cancelled(process.task_id)
            if salvaged is not None:
                self._narrative.extend(salvaged)
            status = TaskStatus.CANCELLED
        await self._record_run_usage(process, terminal)
        await self._report_outcome(task, status)
        return status, task

    async def _record_run_usage(self, process: _Process, terminal: LoopEvent) -> None:
        """Ledger the run's accumulated tokens, outside any unit of work.

        Failed and cancelled runs spent their tokens too; only a run that
        produced a visible final counts as an assistant message.
        """
        if self._limits is None:
            return
        answered = isinstance(terminal, Finished) and bool(terminal.message.content.strip())
        if not answered and process.spent_prompt == 0 and process.spent_completion == 0:
            return  # nothing spent, nothing said — no event
        await self._limits.record(
            UsageEvent(
                user_id=self._dialog.user_id,
                kind=UsageKind.LLM_ANSWER,
                origin=process.origin,
                prompt_tokens=process.spent_prompt,
                completion_tokens=process.spent_completion,
                quantity=1 if answered else 0,
                dialog_id=self._dialog.id,
                exchange_id=process.exchange_id,
                task_id=process.task_id,
            )
        )

    def _delivery_is_certain(self, process: _Process, content: str) -> bool:
        """Whether this outcome is already in the user's hands.

        Two cases, and only these: the terminal reached a live subscriber
        queue, or the result is deliberately empty and there is nothing to
        show. Both are settled by the time the run ends, so delivery can be
        stamped in the same write that ends it.

        Everything else goes through the outbox and is stamped only after it
        has actually been broadcast — stamping early would stop the
        redelivery sweep on a result nobody received.
        """
        return not content.strip() or (
            process.exchange_id is not None and process.terminal_accepted
        )

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

    async def _salvage_interrupted_turn(
        self, process: _Process
    ) -> tuple[ChatMessage, ChatMessage] | None:
        """Persist a cancelled run's partial answer, flagged as incomplete.

        Only the run's own messages (the private suffix) are salvageable.
        The pair is persisted atomically: the note must never be orphaned nor
        observed without the message it annotates (the compactor's tail
        snapshot relies on the pair being indivisible). Returns the pair for
        the caller to put into the in-memory narrative — after its unit of
        work commits, not before.
        """
        last = _latest_assistant_with_content(process.branch[process.synced_len :])
        if last is None:
            return None
        salvaged = replace(last, task_id=process.task_id)
        note = ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE)
        await self._messages.append_pair(self._dialog.id, salvaged, note)
        return salvaged, note

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
    def _evict_and_put(queue: SubscriberQueue, envelope: ConversationEvent) -> bool:
        """Make room for a critical event by evicting the oldest DROPPABLE one.

        Blind head-eviction could evict an earlier critical event (a terminal
        whose delivery the store already recorded) to admit a later one. The
        queue is drained, the oldest non-critical entry is dropped, order is
        preserved. With nothing droppable the put is refused: the caller
        counts the event as not accepted, so the outbox keeps the delivery
        queued instead of stamping it delivered.
        """
        drained: list[ConversationEvent | None] = []
        while True:
            try:
                drained.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        victim = next(
            (
                i
                for i, item in enumerate(drained)
                # the end-of-stream marker is never a victim: a transport that
                # loses it waits forever on a runner that has already gone
                if item is not None and not isinstance(item.payload, _CRITICAL_EVENTS)
            ),
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


def _finished_build(build: "asyncio.Task[ConversationRunner]") -> "ConversationRunner | None":
    """The runner a build produced, or None if it is unfinished or failed."""
    if not build.done() or build.cancelled() or build.exception() is not None:
        return None
    return build.result()


@dataclass(frozen=True, slots=True)
class ManagerStores:
    """Persistence collaborators of one conversation manager.

    `uow` groups the store calls of one phase into one transaction; it must
    be built over the same database the SQL stores write. The default is the
    null unit — correct for the in-memory stores, which have no transactions
    to group.
    """

    dialogs: DialogRepository
    messages: MessageRepository
    tasks: TaskStore
    exchanges: ExchangeRepository
    claims: ClaimRepository
    uow: UnitOfWork = field(default_factory=lambda: UnitOfWork(None))


@dataclass(frozen=True, slots=True)
class OwnershipConfig:
    """How this process names itself as a dialog owner, and how it paces that.

    `node_id` must be stable across this instance's own restarts — seeing its
    own claim after a restart is what lets it reclaim its own stranded work
    immediately instead of waiting out `stale_after_seconds` — and unique
    against every other instance sharing the database, or two of them will
    treat each other's live dialogs as abandoned.
    """

    node_id: str
    heartbeat_seconds: float = CLAIM_HEARTBEAT_SECONDS
    stale_after_seconds: float = CLAIM_STALE_AFTER_SECONDS


class ConversationManager:
    """Owns one runner per dialog, keyed by (user_id, channel).

    Also owns dialog ownership itself: it claims a dialog when it builds the
    actor, keeps the claim warm with a heartbeat, and stands the actor down
    when the heartbeat reports that somebody else took over. The actor knows
    only its own claim — the lifecycle lives here.
    """

    def __init__(
        self,
        config: RunnerConfig,
        stores: ManagerStores,
        ownership: OwnershipConfig,
    ) -> None:
        self._config = config
        self._dialogs = stores.dialogs
        self._messages = stores.messages
        self._tasks = stores.tasks
        self._exchanges = stores.exchanges
        self._claims = stores.claims
        self._uow = stores.uow
        self._node_id = ownership.node_id
        self._heartbeat_seconds = ownership.heartbeat_seconds
        self._stale_after_seconds = ownership.stale_after_seconds
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._surface: DialogSurface | None = None
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
            ours = build is None
            if build is None:
                build = asyncio.create_task(self._build_runner(user_id, channel))
                self._builds[key] = build
        try:
            runner = await asyncio.shield(build)
        except BaseException:
            # drop the memo only when the BUILD itself died — a caller
            # cancelled mid-await (shield keeps the build running) must not
            # evict a build that other callers are about to share
            if build.done() and (build.cancelled() or build.exception() is not None):
                async with self._lock:
                    if self._builds.get(key) is build:
                        del self._builds[key]
            raise
        if ours:
            # After the build, not inside it: a surface resolves runners
            # through this manager, so attaching from within the build would
            # make it await the task it is running in — a dialog that hangs
            # on first contact forever. Only the caller that started the
            # build attaches, so concurrent ones do not attach twice.
            await self._attach_surface(runner)
        return runner

    async def _build_runner(self, user_id: str, channel: str) -> ConversationRunner:
        # one transaction: finding the dialog and claiming it are the same
        # step of the same build — and beside a remote database, a merged
        # BEGIN/COMMIT is a round trip saved on every cold contact.
        # Claimed before the actor exists: building one is what makes this
        # process the dialog's owner, and a previous owner elsewhere learns
        # it was replaced from the bumped generation
        async with self._uow():
            dialog = await self._dialogs.get_or_create(user_id, channel)
            claim = await self._claims.claim(dialog.id, self._node_id)
        # only the hot slice lives in memory: everything up to the compaction
        # boundary is reachable through summaries and history_search. One
        # query — the boundary rides in as a subquery, because its value was
        # never wanted for anything except this very question.
        history = await self._messages.list_hot_slice(dialog.id)
        # no awaits past this point: the runner is registered in the same
        # event-loop step it starts in, so a cancelled build cannot leak a
        # started actor
        runner = ConversationRunner(
            dialog=dialog,
            config=self._config,
            stores=_RunnerStores(
                messages=self._messages,
                tasks=self._tasks,
                exchanges=self._exchanges,
                claims=self._claims,
                uow=self._uow,
            ),
            history=history,
            claim=claim,
        )
        runner.start()
        self._runners[dialog.id] = runner
        # Claiming a dialog means inheriting whatever its previous owner left
        # behind — a handover strands work exactly as a crash does, and no
        # startup sweep will come back for it: this process now holds a fresh
        # claim, so every peer's recovery skips the dialog. Done before the
        # runner is handed out, so the caller's message meets a consistent
        # dialog.
        await self._recover_dialog(runner)
        return runner

    async def _attach_surface(self, runner: ConversationRunner) -> None:
        """Let the configured surface start rendering this dialog.

        Never fatal: a transport that cannot attach costs delivery through
        that transport, not the dialog. The API subscription path is
        untouched either way.
        """
        if self._surface is None:
            return
        try:
            await self._surface.attach(runner)
        except Exception:
            logger.exception("surface attach failed: dialog=%s", runner.dialog_id)

    async def _detach_surface(self, runner: ConversationRunner) -> None:
        """Let the surface stop rendering a dialog that is leaving this process."""
        if self._surface is None:
            return
        try:
            await self._surface.detach(runner)
        except Exception:
            logger.exception("surface detach failed: dialog=%s", runner.dialog_id)

    async def _recover_dialog(self, runner: ConversationRunner) -> None:
        """Pick up the work this dialog's previous owner could not finish.

        The whole of recovery for one dialog, used both when a process starts
        and when it takes a dialog over — the two are the same situation seen
        from different sides. Never raises: a dialog that cannot be recovered
        must still be usable, and every step is idempotent, so the next claim
        tries again.
        """
        dialog_id = runner.dialog_id
        # side by side: the exchange reset and the task lookup touch different
        # tables and neither reads what the other writes — and each helper
        # already swallows its own failure, so the gather cannot reject
        (reopened, stranded), (orphaned, undelivered) = await asyncio.gather(
            self._reopen_and_list_stranded(dialog_id),
            self._for_recovery(dialog_id),
        )
        for task in orphaned:
            try:
                await runner.restart_task(task)
            except Exception:
                logger.exception("orphaned task restart failed: task=%s", task.id)
        # after the orphan restarts on purpose: the sweep re-derives what is
        # still unowned at revive time, so restarted work is not re-answered.
        # `stranded` (read before the restarts) only decides whether to sweep
        # at all — a restart never makes a new exchange unowned, so an empty
        # list stays empty.
        revived = 0
        if stranded:
            try:
                await runner.resume_stranded()
                revived = len(stranded)
            except Exception:
                logger.exception("stranded exchange revive failed: dialog=%s", dialog_id)
        for task in undelivered:
            runner.request_result_delivery(task.id)
        if reopened or revived:
            logger.info(
                "recovered on claim: dialog=%s reopened=%s revived=%s",
                dialog_id,
                reopened,
                revived,
            )

    async def _runner_for_background(self, user_id: str, channel: str) -> ConversationRunner | None:
        """The dialog's runner for work nobody asked this process to do.

        Background work — a settled collection, a cron firing — is found by
        sweeping the whole database, so every instance sees every candidate.
        Routing it through `get_or_create_runner` would make each of them
        *take* the dialog, because claiming is what building a runner does:
        one cron job would move a conversation to whichever instance won the
        lease, and the collecting sweep would bounce dialogs between
        instances every tick. Neither is placement — nothing decided the
        dialog should move; the sweep merely got there first.

        So the rule for background work is the opposite of the rule for a
        message: act on the dialogs we already hold, adopt the ones nobody
        holds, and leave a live peer's alone — it sweeps too, and its own
        tick will pick this up.

        With one instance `held_elsewhere` never matches (our own claims are
        never returned), so this is exactly `get_or_create_runner`.
        """
        dialog = await self._dialogs.get_or_create(user_id, channel)
        existing = self._runners.get(dialog.id)
        if existing is not None:
            return existing  # ours already; the heartbeat stands it down if that changes
        if await self._held_elsewhere(frozenset({dialog.id})):
            return None
        return await self.get_or_create_runner(user_id, channel)

    async def promote_collection(self, user_id: str, channel: str, exchange_id: str) -> None:
        """Hand a settled material collection to its dialog (sweep entry point)."""
        runner = await self._runner_for_background(user_id, channel)
        if runner is None:
            return  # the instance that owns the dialog promotes it on its own tick
        await runner.promote_collected(exchange_id)

    async def wake(
        self,
        user_id: str,
        channel: str,
        title: str,
        prompt: str,
        cron_job_id: str,
    ) -> WakeOutcome:
        """Deliver a cron firing into the user's dialog as a background process.

        See `WakeOutcome`: only DELIVERED is a fire, and NOT_OURS asks the
        scheduler to hand the job back rather than move the dialog here.
        """
        runner = await self._runner_for_background(user_id, channel)
        if runner is None:
            return WakeOutcome.NOT_OURS
        started = await runner.wake(title, prompt, cron_job_id)
        return WakeOutcome.DELIVERED if started else WakeOutcome.LIMITED

    def use_surface(self, surface: DialogSurface) -> None:
        """Bind the transport that renders dialogs of its channel.

        Set after construction rather than through `RunnerConfig` because the
        two need each other: a surface resolves runners through this manager,
        and this manager attaches that surface to every runner it builds.
        Safe because no runner exists before the composition root finishes.
        """
        self._surface = surface

    def start(self) -> None:
        """Start the claim heartbeat; call once, after `recover_interrupted`."""
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._run_heartbeat())

    async def _run_heartbeat(self) -> None:
        """Keep this process's claims warm and stand down whatever it lost.

        The heartbeat carries both halves of ownership: refreshing a claim
        tells recovery elsewhere that this dialog is alive, and failing to
        refresh one is how this process learns the dialog moved. Failures are
        swallowed — a database blip must not stand down healthy actors, and
        the next tick retries.
        """
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                await self._beat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("claim heartbeat failed")

    async def _beat_once(self) -> None:
        """Refresh every live runner's claim; stand down the preempted ones."""
        runners = tuple(self._runners.values())
        if not runners:
            return
        claims: DialogClaimList = [runner.claim for runner in runners]
        kept = await self._claims.heartbeat(claims)
        for runner in runners:
            if runner.dialog_id not in kept:
                await self._drop_preempted(runner)

    async def _drop_preempted(self, runner: ConversationRunner) -> None:
        """Deregister and stand down a runner whose dialog moved elsewhere."""
        async with self._lock:
            for key, build in tuple(self._builds.items()):
                if _finished_build(build) is runner:
                    del self._builds[key]
            self._runners.pop(runner.dialog_id, None)
        await self._detach_surface(runner)
        try:
            await runner.stand_down()
        except Exception:  # a failing runner must not stall the heartbeat
            logger.exception("stand-down failed: dialog=%s", runner.dialog_id)

    async def recover_interrupted(self) -> None:
        """Pick up work stranded by whatever ran before this process started.

        Processes live in memory, so a restart strands PENDING/RUNNING tasks
        forever and loses results persisted but not yet delivered. Runs once
        at startup, before the scheduler and the surfaces come up.

        **Only dialogs no live process owns are touched.** This used to sweep
        the whole database on the reasoning that a restart kills every
        process — true while there was one, and destructive with two: a
        starting instance would reopen exchanges its peers are answering
        right now and restart their tasks as its own. Candidates come from
        the work itself rather than from the claim table, so rows stranded
        before claims existed at all are still recovered.

        The recovery itself is not here: building a dialog's runner is what
        recovers it (`_recover_dialog`), because taking a dialog over and
        starting up are the same situation seen from two sides. This method
        only decides *which* dialogs this process may take.
        """
        candidates = frozenset(await self._list_stranded_dialog_ids()).union(
            task.dialog_id for task in (*await self._orphaned(None), *await self._undelivered(None))
        )
        mine = candidates - await self._held_elsewhere(candidates)
        recovered = 0
        for dialog_id in mine:
            if await self._adopt(dialog_id):
                recovered += 1
        logger.info(
            "startup recovery: dialogs=%s skipped=%s failed=%s",
            recovered,
            len(candidates) - len(mine),
            len(mine) - recovered,
        )

    async def _adopt(self, dialog_id: str) -> bool:
        """Build the dialog's runner, which recovers it; False if that failed."""
        try:
            dialog = await self._dialogs.get(dialog_id)
            await self.get_or_create_runner(dialog.user_id, dialog.channel)
        except Exception:  # recovery must never take the app down
            logger.exception("dialog recovery failed: dialog=%s", dialog_id)
            return False
        return True

    async def _held_elsewhere(self, dialog_ids: frozenset[str]) -> frozenset[str]:
        """Dialogs another live process owns; recovery must not touch these.

        On failure every candidate is treated as somebody else's. Skipping
        recovery costs a delay — the next restart or the owning process picks
        the work up — while recovering a dialog another instance is actively
        running corrupts a live conversation.
        """
        if not dialog_ids:
            return frozenset()
        stale_before = utc_now() - timedelta(seconds=self._stale_after_seconds)
        try:
            return await self._claims.held_elsewhere(dialog_ids, self._node_id, stale_before)
        except Exception:
            logger.exception("claim lookup failed; skipping recovery this start")
            return dialog_ids

    async def _list_stranded_dialog_ids(self) -> list[str]:
        try:
            return await self._exchanges.list_stranded_dialog_ids()
        except Exception:
            logger.exception("stranded dialog sweep failed")
            return []

    async def _for_recovery(self, dialog_id: str | None) -> tuple[TaskList, TaskList]:
        """`(orphaned, undelivered)` for this dialog; empty on failure."""
        try:
            return await self._tasks.list_for_recovery(dialog_id)
        except Exception:
            logger.exception("task recovery sweep failed: dialog=%s", dialog_id)
            return [], []

    async def _orphaned(self, dialog_id: str | None) -> TaskList:
        try:
            return await self._tasks.list_orphaned(dialog_id)
        except Exception:
            logger.exception("orphaned task sweep failed: dialog=%s", dialog_id)
            return []

    async def _undelivered(self, dialog_id: str | None) -> TaskList:
        try:
            return await self._tasks.list_undelivered(dialog_id)
        except Exception:
            logger.exception("undelivered task sweep failed: dialog=%s", dialog_id)
            return []

    async def _reopen_and_list_stranded(self, dialog_id: str) -> tuple[int, ExchangeList]:
        """Reset the dialog's obligations whose owner died; return `(reopened, stranded)`.

        An IN_PROGRESS exchange of a dialog nobody live owns is stale: its
        executor lived in a process that is gone. A settle that failed
        mid-write would otherwise strand it forever, invisible to the
        OPEN-based predicate. AWAITING_USER is left alone: that one waits for
        a human. `stranded` — the OPEN exchanges without a live task, the
        just-reopened ones included — comes from the same transaction,
        because recovery always asks both questions together.
        """
        try:
            return await self._exchanges.reopen_and_list_stranded(dialog_id)
        except Exception:  # recovery must never take the app down
            logger.exception("stranded exchange sweep failed: dialog=%s", dialog_id)
            return 0, []

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
        await self._detach_surface(runner)
        try:
            await runner.stop()
        except Exception:  # a failing runner must not block the deletion
            logger.exception("runner stop failed: dialog=%s", runner.dialog_id)
        await self._release(runner)

    async def stop_all(self) -> None:
        """Stop and deregister every live runner (the app is shutting down).

        Claims are released on the way out, so the dialogs this process was
        running are immediately free for another one — a clean shutdown must
        not make its dialogs wait out the staleness window.
        """
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
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
            await self._detach_surface(runner)
            try:
                await runner.stop()
            except Exception:  # one failing runner must not block the shutdown
                logger.exception("runner stop failed: dialog=%s", runner.dialog_id)
            await self._release(runner)

    async def _release(self, runner: ConversationRunner) -> None:
        """Drop the runner's claim; a claim already taken over is left alone."""
        claim = runner.claim
        try:
            await self._claims.release(claim.dialog_id, claim.owner, claim.generation)
        except Exception:  # shutdown must not fail on a lost database
            logger.exception("claim release failed: dialog=%s", claim.dialog_id)
