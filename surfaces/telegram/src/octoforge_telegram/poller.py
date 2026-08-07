"""Long-poll loop consuming Telegram updates and dispatching them to chat bridges."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
from octoforge_core.agent.runner import ConversationRunner
from octoforge_core.domain import Attachment, AttachmentKind, MessageKind
from octoforge_core.identity.api import IdentityStore, UserStatus
from octoforge_core.identity.service import AccessService
from octoforge_core.media.api import (
    MediaOutcome,
    MediaResult,
    MediaUnderstanding,
    none_understood,
    refused_by_plan,
)
from octoforge_core.time import utc_now

from octoforge_telegram.bridge import RunnerProvider, TelegramBridge, TelegramBridgeOptions
from octoforge_telegram.client import (
    ACTIVITY_INTERVAL_SECONDS,
    CHAT_ACTION_TYPING,
    TELEGRAM_CHANNEL,
    USER_ID_PREFIX,
    TelegramApiError,
    TelegramClient,
)
from octoforge_telegram.drafts import DraftStore
from octoforge_telegram.gateway import AccessRefusedError, GatewayRegistry
from octoforge_telegram.images import REF_PREFIX
from octoforge_telegram.invites.api import (
    InviteAlreadyClaimedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteStatus,
    InviteStore,
    MemberDirectory,
    ReferralStore,
)
from octoforge_telegram.models import (
    TelegramAudioRef,
    TelegramChatType,
    TelegramImageRef,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
    display_name,
)

logger = logging.getLogger(__name__)

COMMAND_START = "/start"
COMMAND_SECRETS = "/secrets"
COMMAND_INVITE = "/invite"
# what the client shows behind its "/" menu button; /start is deliberately
# absent — it is the entry handshake, not something a member ever needs again
SECRETS_MENU_DESCRIPTION = "Передать пароль или токен через защищённую форму"
INVITE_MENU_DESCRIPTION = "Получить персональную ссылку-приглашение"
# what a personal referral payload looks like in `/start ref_<code>`: the
# prefix keeps referral codes and operator invite codes in separate namespaces
REFERRAL_PREFIX = "ref_"
# Stopping an answer is a request like any other — the router recognises it
# and cancels the exchanges the user meant. There is no stop command: one
# would be a second way to say the same thing, and a worse one, because it
# cannot say WHICH of several running answers to stop.
GREETING_TEXT = (
    "Привет! Я OctoForge — напиши вопрос, и я постараюсь помочь. "
    "Чтобы прервать ответ, так и напиши: «стой»."
)
WELCOME_TEXT = (
    "Код принят, добро пожаловать! Я OctoForge — напиши вопрос, и я постараюсь помочь. "
    "Чтобы прервать ответ, так и напиши: «стой»."
)
ACCESS_DENIED_TEXT = (
    "Нет доступа: обратитесь к администратору за инвайт-кодом и отправьте /start <код>."
)
INVITE_INVALID_TEXT = (
    "Этот код недействителен или уже использован. Обратитесь к администратору за новым кодом."
)
TEXT_ONLY_NOTICE = "Пока понимаю только текстовые сообщения."
# forwarded content enters the dialog attributed: the agent must never mistake
# someone else's words for the user's own request
MATERIAL_ATTRIBUTION_TEMPLATE = "[переслано от {origin}]"
MATERIAL_ATTRIBUTION_ANONYMOUS = "[переслано]"
MATERIAL_PLACEHOLDER = "(вложение без текста)"
# marks an ingested image description in the narrative, same spirit as the
# forward attribution tags above; an album is numbered so the agent can tell
# "the second page" from "the first" and knows how many it was given
IMAGE_TAG = "[изображение]"
IMAGE_TAG_NUMBERED = "[изображение {index}/{total}]"
IMAGE_FAILED_PLACEHOLDER = "(не удалось распознать это изображение)"
CAPTION_LABEL = "Подпись:"
# what the cheap tier must print instead of silently stopping mid-word; the
# agent reads it as "there is more on this picture than you were told" and
# `image_look` is the documented way to get the rest. The prompt that asks
# for it lives with the model call, in `octoforge_core.media.service`.
INGESTION_TRUNCATION_MARKER = "[текст на изображении обрезан]"
# an album arrives as N separate updates; this is how long a worker waits
# for the next item of the burst before submitting what it has (Telegram
# often splits an album across two long-poll batches)
ALBUM_QUIET_SECONDS = 1.5
# a user's worker exits after this long with nothing to do, so an idle
# dialog costs no parked task; the next message starts a fresh one
INBOX_IDLE_SECONDS = 60.0
# ingestion (download + describe) is slow network work: the per-user queue
# serializes it within a dialog, this bounds it across all of them
MAX_CONCURRENT_INGESTIONS = 4
# a backlog this deep means a flood or a bug, not a person typing: no cap is
# applied (the user's words are never dropped silently), it is only reported
INBOX_BACKLOG_WARNING = 50
# marks a transcribed recording in the narrative: the agent must know the
# words were heard, not typed — misheard words and false starts are expected,
# and an ambiguous transcript is worth a question rather than a guess
VOICE_TAG = "[голосовое]"
# a recording this short is a mis-tap, and a recognizer does not stay silent
# about silence: on an empty 0.2s clip this deployment's model returned a
# confident "Продолжение следует...", with no low-confidence signal at all
VOICE_MIN_SECONDS = 1
SECONDS_PER_MINUTE = 60
DEFAULT_VOICE_MAX_SECONDS = 600.0
VOICE_TOO_SHORT_NOTICE = "Запись слишком короткая — я ничего не услышал. Скажи ещё раз?"
VOICE_TOO_LONG_TEMPLATE = (
    "Эта запись длиннее {limit} минут — столько я за раз не расшифрую. "
    "Пришли фрагмент короче или напиши текстом."
)
VOICE_EMPTY_NOTICE = "Я не разобрал, что сказано в записи. Напиши текстом или запиши ещё раз?"
VOICE_PLAN_NOTICE = "Распознавание голосовых не входит в твой тариф — напиши, пожалуйста, текстом."
VISION_PLAN_NOTICE = (
    "Распознавание изображений не входит в твой тариф — опиши, пожалуйста, словами."
)
# the queue notice never names a position: the number is not a promise the
# installation can keep, and watching it move (or not) is worse than waiting
QUEUE_WAIT_TEXT = (
    "Сейчас все места заняты — я записал тебя в очередь на доступ. "
    "Напишу, как только место освободится."
)
ACCESS_CLOSED_TEXT = "Доступ закрыт."
REFERRAL_LINK_TEXT = (
    "Твоя персональная ссылка-приглашение:\n{link}\n\n"
    "Отправь её другу — по ней он попадёт в бота (если все места заняты, "
    "встанет в очередь)."
)
REFERRAL_CODE_TEXT = (
    "Твой персональный код-приглашение: {code}\n"
    # the mixed-script line trips the confusable-character lint; the text is
    # ordinary Russian around a latin command
    "Друг активирует его, отправив боту /start {code} (если все места "  # noqa: RUF001
    "заняты, встанет в очередь)."
)
REFERRALS_DISABLED_TEXT = "Приглашения не настроены на этой инсталляции."
SECRETS_LINK_TEXT = (
    "Ссылка на форму секретов (действует 10 минут):\n{url}\n\n"
    "Значения шифруются, ассистент видит только коды секретов. "
    "Никогда не присылайте секреты сообщением в чат."
)
SECRETS_DISABLED_TEXT = "Хранилище секретов не настроено на этой инсталляции."
GROUP_NOTICE = "Пока работаю только в личных чатах."
DEFAULT_ERROR_BACKOFF_SECONDS = 5.0
DRAIN_TIMEOUT_SECONDS = 0.0
LAST_UPDATE_OFFSET = -1


class MembershipDecision(StrEnum):
    """Verdict of the membership gate for one incoming message."""

    ALLOW = "allow"
    ALLOW_WITH_WELCOME = "allow_with_welcome"
    DENY_INVITE_INVALID = "deny_invite_invalid"
    DENY_NO_ACCESS = "deny_no_access"


class TelegramMembership:
    """Invite gate: who may talk to the bot at all.

    Admins (their numeric Telegram id in `admin_ids`) always pass. Anyone
    else needs a way in: an already claimed invite, a fresh operator code
    passed as `/start <code>` (claimed atomically on entry), or — when the
    referral store is wired — a member's personal `/start ref_<code>` link,
    which records who brought whom and stays reusable. A referral opens this
    gate only; the active-user cap behind it queues the invitee like anyone
    else.
    """

    def __init__(
        self,
        invite_store: InviteStore,
        admin_ids: Iterable[int],
        referrals: ReferralStore | None = None,
        open_registration: bool = False,
    ) -> None:
        self._invites = invite_store
        self._admin_ids = frozenset(admin_ids)
        self._referrals = referrals
        self._open = open_registration

    async def check(self, user_id: str, text: str) -> MembershipDecision:
        """Decide whether the user may proceed; claims `/start <code>` invites.

        With registration open nobody is turned away here — codes still work
        and still record who brought whom, but a bad one no longer closes the
        door, because the door is not what limits anything any more. How many
        people get in is then the active-user cap's business, one layer down.
        """
        numeric_id = chat_id_from_user_id(user_id)
        if numeric_id is not None and numeric_id in self._admin_ids:
            return MembershipDecision.ALLOW
        decision = await self._decide(user_id, text)
        if self._open and decision is not MembershipDecision.ALLOW_WITH_WELCOME:
            return MembershipDecision.ALLOW
        return decision

    async def _decide(self, user_id: str, text: str) -> MembershipDecision:
        code = _start_code(text)
        if code is not None and code.startswith(REFERRAL_PREFIX) and self._referrals is not None:
            return await self._claim_referral(code.removeprefix(REFERRAL_PREFIX), user_id)
        if code is not None:
            return await self._claim(code, user_id)
        if await self._is_member(user_id):
            return MembershipDecision.ALLOW
        return MembershipDecision.DENY_NO_ACCESS

    async def _is_member(self, user_id: str) -> bool:
        invite = await self._invites.get_by_user(user_id)
        if invite is not None and invite.status is InviteStatus.CLAIMED:
            return True
        return self._referrals is not None and await self._referrals.claim_of(user_id) is not None

    async def _claim(self, code: str, user_id: str) -> MembershipDecision:
        try:
            await self._invites.claim(code, user_id)
        except (InviteAlreadyClaimedError, InviteExpiredError, InviteNotFoundError):
            return MembershipDecision.DENY_INVITE_INVALID
        return MembershipDecision.ALLOW_WITH_WELCOME

    async def _claim_referral(self, code: str, user_id: str) -> MembershipDecision:
        """A member's reusable link: attribute once, never gate a member out.

        Self-referral is invalid — a way in must not be mintable from
        inside one's own pocket; a member clicking somebody's link passes
        (they are a member) but their original attribution stands.
        """
        assert self._referrals is not None  # only called when wired
        owner = await self._referrals.owner_of(code)
        if owner is None or owner == user_id:
            return MembershipDecision.DENY_INVITE_INVALID
        if await self._is_member(user_id):
            return MembershipDecision.ALLOW
        await self._referrals.record_claim(user_id, owner, code)
        return MembershipDecision.ALLOW_WITH_WELCOME


def _start_code(text: str) -> str | None:
    """Extract the invite code from `/start <code>` (None for a bare /start)."""
    if not text.startswith(COMMAND_START + " "):
        return None
    return text[len(COMMAND_START) :].strip() or None


class TelegramBridgeRegistry:
    """Owns one bridge per Telegram user id."""

    def __init__(
        self,
        runner_provider: RunnerProvider,
        client: TelegramClient,
        edit_throttle_seconds: float,
        drafts: DraftStore | None = None,
        identities: IdentityStore | None = None,
    ) -> None:
        self._runner_provider = runner_provider
        self._client = client
        # a dialog names a PERSON; which chat to write to is what this
        # surface knows about them, and it lives in their identity
        self._identities = identities
        self._options = TelegramBridgeOptions(
            edit_throttle_seconds=edit_throttle_seconds, drafts=drafts
        )
        self._bridges: dict[str, TelegramBridge] = {}

    async def person_of(self, external_id: str) -> str:
        """The person behind a Telegram account, minted on first contact.

        The gate decided whether they may talk at all; this decides who they
        are. Without an identity store the account id doubles as the person,
        which is what a deployment that never migrated looks like.
        """
        if self._identities is None:
            return f"{USER_ID_PREFIX}{external_id}"
        return await self._identities.resolve_or_create(TELEGRAM_CHANNEL, external_id)

    async def gateway_for(self, handle: str, chat_id: int) -> TelegramBridge:
        """The bridge for the PERSON behind a Telegram handle.

        Bridges are keyed by person, not by account: a dialog belongs to
        somebody, and that is what survives them changing accounts.
        """
        return self.get_or_create(
            await self.person_of(handle.removeprefix(USER_ID_PREFIX)), chat_id
        )

    def get_or_create(self, user_id: str, chat_id: int) -> TelegramBridge:
        """Return the bridge for the user, creating it on first contact."""
        bridge = self._bridges.get(user_id)
        if bridge is None:
            bridge = TelegramBridge(
                user_id=user_id,
                chat_id=chat_id,
                runner_provider=self._runner_provider,
                client=self._client,
                options=self._options,
            )
            self._bridges[user_id] = bridge
        return bridge

    async def attach(self, runner: ConversationRunner) -> None:
        """Render this dialog, if it is one of ours (`DialogSurface`).

        Tied to the actor rather than to a request on purpose: a scheduled run
        finishing while nobody is looking still has to reach the chat, and
        after a dialog moves here from another process there is nobody else
        left to deliver it.
        """
        if runner.channel != TELEGRAM_CHANNEL:
            return
        chat_id = await self._chat_of(runner.user_id)
        if chat_id is None:
            logger.warning("no telegram account on record for user %r", runner.user_id)
            return
        await self.get_or_create(runner.user_id, chat_id).start()

    async def _chat_of(self, user_id: str) -> int | None:
        """Where to write to this person, from their Telegram identity.

        Read rather than derived: the id of a person carries no structure, and
        that is what makes the old trick — parsing the chat back out of who
        they are — impossible rather than merely discouraged.
        """
        if self._identities is None:
            return None
        for identity in await self._identities.identities_of(user_id):
            if identity.surface == TELEGRAM_CHANNEL and identity.active:
                return _as_chat_id(identity.external_id)
        return None

    async def detach(self, runner: ConversationRunner) -> None:
        """Stop rendering a dialog that is leaving this process (`DialogSurface`)."""
        if runner.channel != TELEGRAM_CHANNEL:
            return
        bridge = self._bridges.pop(runner.user_id, None)
        if bridge is not None:
            await bridge.aclose()

    async def aclose(self) -> None:
        """Stop all bridges (on app shutdown)."""
        for bridge in self._bridges.values():
            await bridge.aclose()


@dataclass(slots=True)
class _Inbox:
    """One user's ingestion queue and the worker draining it.

    A plain deque rather than an `asyncio.Queue` because the worker peeks:
    collecting an album means pulling the next message and putting it back
    when it turns out to belong to something else. `arrived` wakes the
    worker; it holds no state beyond "something is there".
    """

    pending: deque[TelegramMessage] = field(default_factory=deque)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    # the worker holds a message it has not finished handling (album
    # collection included): "this dialog still owes the user something"
    busy: bool = False


@dataclass(frozen=True, slots=True)
class TelegramPollerOptions:
    """Behavior knobs of the poller beyond its two collaborators.

    `secrets_link` builds the one-time secrets-form URL for a user id
    (None: the /secrets command reports the feature as not configured).
    `media` turns incoming pictures and recordings into text; None turns
    that off entirely (today's placeholder/text-only behavior, zero extra
    cost) — the composition root is the only place that ever picks a
    concrete implementation, this bundle only sees the port.
    """

    poll_timeout_seconds: float
    error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS
    membership: TelegramMembership | None = None
    secrets_link: Callable[[str], str] | None = None
    # who-is-who mirror: profiles of gated users, refreshed on every contact
    directory: MemberDirectory | None = None
    # the same mirror pushed into core: the identity's name/username follow
    # the Telegram profile, and a person still unnamed is christened by it
    identities: IdentityStore | None = None
    # Turns a picture or a recording into text — through the core, where the
    # plan check and the usage ledger live. None means this deployment does
    # no media understanding at all: pictures keep the placeholder path and
    # recordings the "text only" notice, at zero extra cost.
    media: MediaUnderstanding | None = None
    # the status gate (waiting/active/banned); None = statuses are not
    # enforced on this surface (an HTTP-gateway bot leaves them to the service)
    access: AccessService | None = None
    # personal referral links (/invite); None = the command reports "not set up"
    referrals: ReferralStore | None = None
    # the bot's @handle for building t.me deep links; None = codes are handed
    # out as text with a /start instruction instead
    bot_username: str | None = None
    # how long an album's burst must stay quiet before it is submitted
    album_quiet_seconds: float = ALBUM_QUIET_SECONDS
    voice_max_seconds: float = DEFAULT_VOICE_MAX_SECONDS


class TelegramPoller:
    """Long-poll loop: fetch updates, advance the offset, dispatch to bridges."""

    def __init__(
        self,
        client: TelegramClient,
        registry: GatewayRegistry,
        options: TelegramPollerOptions,
    ) -> None:
        self._client = client
        self._registry = registry
        self._poll_timeout_seconds = options.poll_timeout_seconds
        self._error_backoff_seconds = options.error_backoff_seconds
        self._membership = options.membership
        self._secrets_link = options.secrets_link
        self._directory = options.directory
        self._identities = options.identities
        self._media = options.media
        self._access = options.access
        self._referrals = options.referrals
        self._bot_username = options.bot_username
        # (person, text) → day: one status notice per person per day — the
        # answer does not change by asking, and silence beats a nag
        self._status_notes: dict[tuple[str, str], str] = {}
        self._album_quiet_seconds = options.album_quiet_seconds
        self._voice_max_seconds = options.voice_max_seconds
        self._offset: int | None = None
        # one ingestion queue per user: the poll loop only enqueues, so no
        # dialog's slow work (download, vision, transcription) can stall the
        # updates of any other user
        self._inboxes: dict[str, _Inbox] = {}
        self._ingest_slots = asyncio.Semaphore(MAX_CONCURRENT_INGESTIONS)

    async def run_forever(self) -> None:
        """Poll updates until cancelled.

        Three layers of defense, from innermost out: a `get_updates` failure
        only backs off and retries; a single update failing to dispatch is
        caught in `_dispatch_safely` (and, once queued, in `_handle_safely`)
        and never stops the loop; this top-level catch-all is the last resort
        for whatever escapes both (an invites-store DB blip, a bug in code we
        didn't anticipate) — if it also killed the loop, the whole Telegram
        surface would go dark for every user until a process restart.
        """
        await self._register_commands()
        await self._drain_backlog()
        try:
            while True:
                try:
                    await self._poll_once()
                except Exception:
                    logger.exception("Telegram poll loop hit an unexpected error; retrying")
                    await asyncio.sleep(self._error_backoff_seconds)
        finally:
            for inbox in tuple(self._inboxes.values()):
                if inbox.worker is not None:
                    inbox.worker.cancel()  # shutdown: queued work is dropped

    async def _register_commands(self) -> None:
        """Publish the command menu for whatever this deployment actually wires.

        Registered on every start (the call is idempotent), so the menu always
        mirrors the configuration — including clearing a stale menu when a
        capability was switched off. Best-effort: the menu is a convenience,
        and a Bot API hiccup here must not keep the surface from starting.
        """
        commands: list[tuple[str, str]] = []
        if self._secrets_link is not None:
            commands.append((COMMAND_SECRETS.removeprefix("/"), SECRETS_MENU_DESCRIPTION))
        if self._referrals is not None:
            commands.append((COMMAND_INVITE.removeprefix("/"), INVITE_MENU_DESCRIPTION))
        try:
            await self._client.set_my_commands(commands)
        except Exception:
            logger.warning("Telegram setMyCommands failed; keeping the old menu", exc_info=True)

    async def _poll_once(self) -> None:
        try:
            updates = await self._client.get_updates(self._offset, self._poll_timeout_seconds)
        except (httpx.HTTPError, TelegramApiError):
            logger.warning("Telegram polling failed; retrying", exc_info=True)
            await asyncio.sleep(self._error_backoff_seconds)
            return
        for update in updates:
            self._offset = update.update_id + 1
            await self._dispatch_safely(update)

    async def dispatch(self, update: TelegramUpdate) -> None:
        """Take one update off the loop: queue it for its user's worker.

        This is everything the poll loop does per update, and it must stay
        that way — nothing here awaits anything. Everything else (the invite
        gate, the profile mirror, downloads, vision, the dialog itself)
        happens in `_handle`, inside the user's own worker, so one user's
        two-minute voice message cannot hold up anybody else's text.
        """
        message = update.message
        if message is None or message.from_user is None:
            return
        self._enqueue(f"{USER_ID_PREFIX}{message.from_user.id}", message)

    def _enqueue(self, user_id: str, message: TelegramMessage) -> None:
        """Append the message to its user's queue, starting the worker if needed."""
        inbox = self._inboxes.get(user_id)
        if inbox is None:
            inbox = _Inbox()
            self._inboxes[user_id] = inbox
        inbox.pending.append(message)
        inbox.arrived.set()
        if len(inbox.pending) >= INBOX_BACKLOG_WARNING:
            logger.warning(
                "Telegram ingestion backlog for %s: %d messages waiting",
                user_id,
                len(inbox.pending),
            )
        if inbox.worker is None or inbox.worker.done():
            inbox.worker = asyncio.create_task(self._work(user_id, inbox))
            inbox.worker.add_done_callback(_report_worker_exit)

    async def _work(self, user_id: str, inbox: _Inbox) -> None:
        """Drain one user's queue in order until it stays empty long enough."""
        while True:
            message = await self._next_message(inbox, INBOX_IDLE_SECONDS)
            if message is None:
                # idle: forget the user until they write again, so a process
                # serving many dialogs parks no task and keeps no queue
                inbox.worker = None
                self._inboxes.pop(user_id, None)
                return
            inbox.busy = True
            try:
                batch = [message]
                if message.media_group_id is not None:
                    batch = await self._collect_album(inbox, message)
                await self._handle_safely(user_id, batch)
            finally:
                inbox.busy = False

    async def _next_message(self, inbox: _Inbox, timeout: float) -> TelegramMessage | None:
        """The next queued message, or None when `timeout` passes with none."""
        if not inbox.pending:
            inbox.arrived.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(inbox.arrived.wait(), timeout)
        # the deque is re-read after the wait either way: a message enqueued in
        # the same tick as the timeout must not be left behind until the next one
        return inbox.pending.popleft() if inbox.pending else None

    async def _collect_album(self, inbox: _Inbox, first: TelegramMessage) -> list[TelegramMessage]:
        """Collect the rest of an album, waiting out its burst.

        The queue makes this trivial and ordered by construction: a message
        that is not part of this album goes back to the head and is handled
        right after it, so the pictures can never be overtaken by the
        question about them.
        """
        batch = [first]
        while True:
            message = await self._next_message(inbox, self._album_quiet_seconds)
            if message is None:
                return batch
            if message.media_group_id != first.media_group_id:
                inbox.pending.appendleft(message)
                return batch
            batch.append(message)

    async def _handle_safely(self, user_id: str, batch: list[TelegramMessage]) -> None:
        """Handle one message (or album) with the same catch-all the loop had.

        A worker must survive a poison message: it owns the ordering of one
        dialog, and dying would leave that dialog silent until the next
        message revives it.
        """
        try:
            await self._handle(user_id, batch)
        except asyncio.CancelledError:
            raise  # a stop, or shutdown: abandon this work deliberately
        except AccessRefusedError as refusal:
            # the status gate ran on the service (this process cannot resolve
            # a person), and its verdict has to reach the person as words:
            # swallowing it would leave a queued newcomer in silence
            await self._refuse_access(user_id, batch[0].chat.id, refusal)
        except Exception:
            logger.warning("Failed to handle a Telegram message from %s", user_id, exc_info=True)

    async def _refuse_access(self, user_id: str, chat_id: int, refusal: AccessRefusedError) -> None:
        """Say a service-side refusal out loud; an unknown verdict stays quiet."""
        try:
            status = UserStatus(refusal.status)
        except ValueError:
            logger.warning("service refused %s with an unknown status %r", user_id, refusal.status)
            return
        await self._say_status(user_id, chat_id, status)

    async def _handle(self, user_id: str, batch: list[TelegramMessage]) -> None:
        """Route one message (or one album): notices, gate, forwards, images or text."""
        message = batch[0]
        chat_id = message.chat.id
        if message.chat.type is not TelegramChatType.PRIVATE:
            await self._client.send_message(chat_id, GROUP_NOTICE)
            return
        assert message.from_user is not None  # dispatch drops senderless updates
        if not await self._gate(user_id, chat_id, message):
            return
        if len(batch) > 1 or message.media_group_id is not None:
            await self._dispatch_album(batch, user_id, chat_id)
            return
        if message.forward_origin is not None:
            await self._dispatch_forward(message, user_id, chat_id)
            return
        image = message.best_image
        if image is not None and self._media is not None:
            await self._dispatch_own_image(message, user_id, chat_id, image)
            return
        audio = message.best_audio
        if audio is not None and self._media is not None:
            await self._dispatch_voice(message, user_id, chat_id, audio, kind=MessageKind.OWN)
            return
        await self._dispatch_plain_or_notice(message, user_id, chat_id)

    async def _dispatch_plain_or_notice(
        self, message: TelegramMessage, user_id: str, chat_id: int
    ) -> None:
        """Route by text/caption body: the text-only notice when absent, else the text pipeline.

        This is also the fallback for a photo when vision is off or fails:
        identical to the pre-vision behavior (a caption is treated as plain
        text, a bare photo gets the notice).
        """
        if message.body is None:
            await self._client.send_message(chat_id, TEXT_ONLY_NOTICE)
            return
        reply_to_message_id = (
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        )
        await self._dispatch_text(
            message.message_id, user_id, chat_id, message.body, reply_to_message_id
        )

    async def _dispatch_forward(self, message: TelegramMessage, user_id: str, chat_id: int) -> None:
        """Forwarded material: a picture or a recording is read, else the placeholder path."""
        audio = message.best_audio
        if audio is not None and self._media is not None:
            await self._dispatch_voice(message, user_id, chat_id, audio, kind=MessageKind.MATERIAL)
            return
        image = message.best_image
        if image is not None and self._media is not None:
            results = await self._submit_images(
                message, (image,), user_id=user_id, chat_id=chat_id, kind=MessageKind.MATERIAL
            )
            if refused_by_plan(results):
                await self._refuse_vision(message, user_id, chat_id)
                return
            if not none_understood(results):
                return
            logger.warning("No description for a forwarded image from %s; falling back", user_id)
        await self._dispatch_material(message, user_id, chat_id)

    async def _dispatch_own_image(
        self,
        message: TelegramMessage,
        user_id: str,
        chat_id: int,
        image: TelegramImageRef,
    ) -> None:
        """The user's own photo: described via vision, else today's text/notice path.

        A bare picture is material, not a request: the user shared something
        without saying what they want, so it collects like a forward and the
        agent asks what to do with it. A caption changes that — then the
        caption is the user speaking and the picture is its context.
        """
        kind = MessageKind.OWN if message.body else MessageKind.MATERIAL
        results = await self._submit_images(
            message, (image,), user_id=user_id, chat_id=chat_id, kind=kind
        )
        if refused_by_plan(results):
            await self._refuse_vision(message, user_id, chat_id)
            return
        if not none_understood(results):
            return
        logger.warning("No description for a photo from %s; falling back", user_id)
        await self._dispatch_plain_or_notice(message, user_id, chat_id)

    async def _submit_images(  # noqa: PLR0913 — mirrors everything one submission needs
        self,
        anchor: TelegramMessage,
        images: Sequence[TelegramImageRef],
        *,
        user_id: str,
        chat_id: int,
        kind: MessageKind,
        caption: str | None = None,
    ) -> tuple[MediaResult, ...]:
        """Describe every picture and submit them as ONE message of the dialog.

        `anchor` is the message the entry is attributed to (its id is the
        dedup key and the reply target, its forward origin the attribution);
        for an album that is its first item, and `caption` is whichever item
        carried one. Every attachment is kept even when its description
        failed — `image_look` can still go back to the file itself.

        Returns what the core made of each picture and submits nothing when
        it made nothing: the caller reads the outcomes to choose between
        saying "not in your plan" out loud and falling back to the pre-vision
        path in silence. Nothing is submitted before the descriptions are in
        hand, so a refused user never has a dialog opened on their behalf.
        """
        assert self._media is not None  # only called when the caller already checked
        refs = tuple(f"{REF_PREFIX}{image.file_id}" for image in images)
        async with self._ingest_slots, self._showing_activity(chat_id, kind):
            results = await self._media.describe(user_id, refs)
        if none_understood(results):
            return results
        forwarded = anchor.forward_origin is not None
        origin = anchor.forward_origin.display_name if anchor.forward_origin is not None else ""
        text = _compose_images_message(
            results,
            caption=caption if caption is not None else (anchor.body or ""),
            origin=origin,
            forwarded=forwarded,
        )
        bridge = await self._registry.gateway_for(user_id, chat_id)
        await bridge.handle_text(
            text,
            client_message_id=str(anchor.message_id),
            kind=kind,
            origin=(origin or None) if forwarded else None,
            attachments=tuple(Attachment(kind=AttachmentKind.IMAGE, ref=ref) for ref in refs),
        )
        return results

    async def _dispatch_voice(
        self,
        message: TelegramMessage,
        user_id: str,
        chat_id: int,
        audio: TelegramAudioRef,
        *,
        kind: MessageKind,
    ) -> None:
        """Transcribe a recording and submit the transcript as the message text.

        A voice message the user recorded IS them speaking, so it enters the
        dialog as their own words (`OWN`) and starts a run — the opposite of
        a bare picture, which is material because sharing something is not
        asking for anything. A FORWARDED recording is someone else's voice
        and stays material, attributed like forwarded text.

        The duration bounds travel with the call rather than being applied
        here, so that the plan is answered first: somebody whose tariff has
        no voice must hear that, not "your recording was too short" — no
        length would have worked for them. Nothing is downloaded until both
        have passed; a mis-tap is refused (a recognizer invents confident
        text for silence — measured), and so is a recording over the cap,
        which would also eat the day's transcription quota.
        """
        assert self._media is not None  # only called when the caller already checked
        async with self._ingest_slots, self._showing_activity(chat_id, kind):
            result = await self._media.transcribe(
                user_id,
                f"{REF_PREFIX}{audio.file_id}",
                _declared_seconds(audio),
                VOICE_MIN_SECONDS,
                self._voice_max_seconds,
            )
        if result.outcome is MediaOutcome.REFUSED_BY_PLAN:
            # a caption still reaches the dialog as text, a bare recording
            # ends at the notice (a second "text only" note would be noise)
            await self._client.send_message(chat_id, VOICE_PLAN_NOTICE)
            if message.body is not None:
                await self._dispatch_plain_or_notice(message, user_id, chat_id)
            return
        if result.outcome in (MediaOutcome.TOO_SHORT, MediaOutcome.TOO_LONG):
            await self._client.send_message(chat_id, self._duration_notice(result.outcome))
            return
        if result.outcome is MediaOutcome.UNAVAILABLE:
            logger.warning("No transcript for a recording from %s; falling back", user_id)
            await self._dispatch_without_vision(message, user_id, chat_id)
            return
        transcript = result.text.strip()
        if not transcript:
            await self._client.send_message(chat_id, VOICE_EMPTY_NOTICE)
            return
        forwarded = message.forward_origin is not None
        origin = message.forward_origin.display_name if message.forward_origin is not None else ""
        bridge = await self._registry.gateway_for(user_id, chat_id)
        await bridge.handle_text(
            _compose_voice_message(
                transcript, caption=message.body or "", origin=origin, forwarded=forwarded
            ),
            client_message_id=str(message.message_id),
            reply_to_message_id=(
                message.reply_to_message.message_id
                if message.reply_to_message is not None
                else None
            ),
            kind=kind,
            origin=(origin or None) if forwarded else None,
            attachments=(
                Attachment(kind=AttachmentKind.AUDIO, ref=f"{REF_PREFIX}{audio.file_id}"),
            ),
        )

    async def _gate(self, user_id: str, chat_id: int, message: TelegramMessage) -> bool:
        """Both doors of one entrance: the invite gate, then the status gate.

        The invite gate comes before every reply — a stranger must not be
        able to make the bot answer anything, not even the "text only"
        notice — and only past it is the member recorded, so strangers
        knocking with bad codes stay unrecorded.
        """
        if not await self._check_membership(user_id, chat_id, message.body or ""):
            return False
        assert message.from_user is not None  # the caller already checked
        await self._record_member(user_id, message.from_user)
        return await self._admit(user_id, chat_id)

    async def _admit(self, user_id: str, chat_id: int) -> bool:
        """The status gate: WAITING and BANNED stop here, before any dispatch.

        Runs after the invite gate (a stranger gets nothing, not even a
        queue notice) and resolves the core person to do it — statuses,
        like plans, are filed under the person, never under the Telegram
        handle. `admit` itself grabs a free slot for the waiting, so the
        queue drains as its people knock, with no sweep anywhere.
        """
        if self._access is None:
            return True
        person = await self._registry.person_of(user_id.removeprefix(USER_ID_PREFIX))
        status = await self._access.admit(person)
        if status is UserStatus.ACTIVE:
            return True
        await self._say_status(person, chat_id, status)
        return False

    async def _say_status(self, person: str, chat_id: int, status: UserStatus) -> None:
        """Tell the person why they are not through, at most once a day.

        The answer does not change by asking again, so repeating it every
        message would be a nag; saying it once a day still reaches somebody
        who comes back tomorrow to find the queue moved.
        """
        notice = ACCESS_CLOSED_TEXT if status is UserStatus.BANNED else QUEUE_WAIT_TEXT
        note_key = (person, notice)
        day = utc_now().strftime("%Y-%m-%d")
        if self._status_notes.get(note_key) != day:
            self._status_notes[note_key] = day
            await self._client.send_message(chat_id, notice)

    async def _refuse_vision(self, message: TelegramMessage, user_id: str, chat_id: int) -> None:
        """The plan said no to a picture: one notice, and any text still lands.

        Before a single byte is downloaded, like the voice gate. A forward
        keeps its material path (the placeholder stands in for the picture);
        a caption is the user speaking and reaches the dialog as text; a
        bare photo ends at the notice — a "text only" note on top would just
        be noise.
        """
        await self._client.send_message(chat_id, VISION_PLAN_NOTICE)
        if message.forward_origin is not None:
            await self._dispatch_material(message, user_id, chat_id)
            return
        if message.body is not None:
            await self._dispatch_plain_or_notice(message, user_id, chat_id)

    def _duration_notice(self, outcome: MediaOutcome) -> str:
        """The words for a recording outside the bounds; the limit is ours to name."""
        if outcome is MediaOutcome.TOO_SHORT:
            return VOICE_TOO_SHORT_NOTICE
        return VOICE_TOO_LONG_TEMPLATE.format(
            limit=int(self._voice_max_seconds // SECONDS_PER_MINUTE)
        )

    @asynccontextmanager
    async def _showing_activity(self, chat_id: int, kind: MessageKind) -> AsyncIterator[None]:
        """Keep "typing" alive while slow ingestion runs, for answer-bound work only.

        Without this the user stares at nothing while a picture is described
        or a voice message transcribed — the indicator used to be sent by
        `TelegramBridge.handle_text`, which now runs only once the slow part
        is already over. MATERIAL work stays silent for the same reason the
        bridge skips it there: it promises a reply that is not coming yet.
        """
        if kind is not MessageKind.OWN:
            yield
            return
        pulse = asyncio.create_task(self._pulse_activity(chat_id))
        try:
            yield
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse

    async def _pulse_activity(self, chat_id: int) -> None:
        """Re-send the chat action until cancelled; a failed indicator is not fatal."""
        while True:
            with suppress(TelegramApiError, httpx.HTTPError):
                await self._client.send_chat_action(chat_id, CHAT_ACTION_TYPING)
            await asyncio.sleep(ACTIVITY_INTERVAL_SECONDS)

    async def _dispatch_album(
        self, batch: list[TelegramMessage], user_id: str, chat_id: int
    ) -> None:
        """Submit an album as a single entry: every picture described, one caption.

        An album is one act of the user's ("here are the three pages of the
        menu"), so it must become one message. Split into N, the pages
        without a caption would be material (a caption rides only one item),
        collect in their own exchange and get an answer of their own, while
        the run started by the captioned page would see a single page —
        which is exactly the bug this replaces.
        """
        anchor = batch[0]
        images = tuple(image for image in (item.best_image for item in batch) if image is not None)
        caption = next((item.body for item in batch if item.body), "")
        if self._media is None or not images:
            await self._dispatch_without_vision(anchor, user_id, chat_id)
            return
        forwarded = anchor.forward_origin is not None
        kind = MessageKind.MATERIAL if forwarded or not caption else MessageKind.OWN
        results = await self._submit_images(
            anchor, images, user_id=user_id, chat_id=chat_id, kind=kind, caption=caption
        )
        if refused_by_plan(results):
            await self._refuse_vision(anchor, user_id, chat_id)
            return
        if not none_understood(results):
            return
        logger.warning("No descriptions for an album from %s; falling back", user_id)
        await self._dispatch_without_vision(anchor, user_id, chat_id)

    async def _dispatch_without_vision(
        self, message: TelegramMessage, user_id: str, chat_id: int
    ) -> None:
        """The pre-vision path for a picture: material when forwarded, else text/notice."""
        if message.forward_origin is not None:
            await self._dispatch_material(message, user_id, chat_id)
            return
        await self._dispatch_plain_or_notice(message, user_id, chat_id)

    async def _dispatch_material(
        self, message: TelegramMessage, user_id: str, chat_id: int
    ) -> None:
        """Hand a forwarded message to the dialog as material, never as a question."""
        origin = message.forward_origin.display_name if message.forward_origin is not None else ""
        body = message.body
        content = body if body is not None else MATERIAL_PLACEHOLDER
        attribution = (
            MATERIAL_ATTRIBUTION_TEMPLATE.format(origin=origin)
            if origin
            else MATERIAL_ATTRIBUTION_ANONYMOUS
        )
        bridge = await self._registry.gateway_for(user_id, chat_id)
        await bridge.handle_text(
            f"{attribution} {content}",
            client_message_id=str(message.message_id),
            kind=MessageKind.MATERIAL,
            origin=origin or None,
        )

    async def _dispatch_text(
        self,
        message_id: int,
        user_id: str,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Route an allowed text: surface commands first, then the dialog bridge."""
        if text == COMMAND_START or _start_code(text) is not None:
            if text == COMMAND_START:
                await self._client.send_message(chat_id, GREETING_TEXT)
            return  # a successful claim was already welcomed by the gate
        if text.strip() == COMMAND_SECRETS:
            # intercepted BEFORE the dialog pipeline: the command (and the
            # secrets themselves, entered on the linked form) never reach the
            # narrative, the archive or the LLM
            await self._send_secrets_link(user_id, chat_id)
            return
        if text.strip() == COMMAND_INVITE:
            await self._send_referral_link(user_id, chat_id)
            return
        bridge = await self._registry.gateway_for(user_id, chat_id)
        # the chat-level message id, not update_id: it doubles as the reply
        # target when the answer threads back to this question, and it is
        # unique per chat — enough for the (dialog, client_message_id) dedup
        await bridge.handle_text(
            text, client_message_id=str(message_id), reply_to_message_id=reply_to_message_id
        )

    async def _send_secrets_link(self, user_id: str, chat_id: int) -> None:
        """Hand out a form link for this ACCOUNT; the service resolves the person.

        The account id, not the handle and not the person: ingestion may be
        running out of process, where nothing knows who an account belongs
        to. Passing the handle put the form in a namespace of its own —
        secrets written under `tg:<account>`, read back under the person, and
        nothing anywhere reported a problem.
        """
        if self._secrets_link is None:
            await self._client.send_message(chat_id, SECRETS_DISABLED_TEXT)
            return
        external_id = user_id.removeprefix(USER_ID_PREFIX)
        await self._client.send_message(
            chat_id, SECRETS_LINK_TEXT.format(url=self._secrets_link(external_id))
        )

    async def _send_referral_link(self, user_id: str, chat_id: int) -> None:
        """Hand out the member's personal reusable link.

        Only the admitted reach this command — the status gate ran first —
        so a waiting user cannot mint links into the queue.
        """
        if self._referrals is None:
            await self._client.send_message(chat_id, REFERRALS_DISABLED_TEXT)
            return
        code = f"{REFERRAL_PREFIX}{await self._referrals.code_of(user_id)}"
        if self._bot_username:
            link = f"https://t.me/{self._bot_username}?start={code}"
            await self._client.send_message(chat_id, REFERRAL_LINK_TEXT.format(link=link))
            return
        await self._client.send_message(chat_id, REFERRAL_CODE_TEXT.format(code=code))

    async def _record_member(self, user_id: str, user: TelegramUser) -> None:
        """Mirror the sender's profile; recording must never break dispatch."""
        if self._directory is not None:
            try:
                await self._directory.record(
                    user_id, user.first_name, user.last_name, user.username
                )
            except Exception:
                logger.exception("member profile record failed: user=%s", user_id)
        if self._identities is not None:
            try:
                await self._identities.update_profile(
                    TELEGRAM_CHANNEL,
                    user_id.removeprefix(USER_ID_PREFIX),
                    display_name(user),
                    user.username,
                )
            except Exception:
                logger.exception("identity profile update failed: user=%s", user_id)

    async def _check_membership(self, user_id: str, chat_id: int, text: str) -> bool:
        """Apply the invite gate (no gate configured = everyone passes)."""
        if self._membership is None:
            return True
        decision = await self._membership.check(user_id, text)
        if decision is MembershipDecision.ALLOW:
            return True
        if decision is MembershipDecision.ALLOW_WITH_WELCOME:
            await self._client.send_message(chat_id, WELCOME_TEXT)
            return True
        if decision is MembershipDecision.DENY_INVITE_INVALID:
            await self._client.send_message(chat_id, INVITE_INVALID_TEXT)
            return False
        await self._client.send_message(chat_id, ACCESS_DENIED_TEXT)
        return False

    async def _dispatch_safely(self, update: TelegramUpdate) -> None:
        """Dispatch one update; any failure is logged and swallowed.

        A broad `Exception` catch is deliberate: one bad update (a poison
        payload, an invites-store DB blip, an unexpected bug) must never take
        the whole polling loop down with it — that would go dark for every
        Telegram user until a process restart. `asyncio.CancelledError` is a
        `BaseException`, not an `Exception`, so shutdown still cancels cleanly.
        """
        try:
            await self.dispatch(update)
        except Exception:
            logger.warning("Failed to dispatch Telegram update %s", update.update_id, exc_info=True)

    async def _drain_backlog(self) -> None:
        """Skip updates that accumulated while the app was down (best effort)."""
        try:
            updates = await self._client.get_updates(LAST_UPDATE_OFFSET, DRAIN_TIMEOUT_SECONDS)
        except (httpx.HTTPError, TelegramApiError):
            logger.warning("Failed to drain the Telegram update backlog", exc_info=True)
            return
        if updates:
            self._offset = updates[-1].update_id + 1


def _report_worker_exit(worker: asyncio.Task[None]) -> None:
    """Log a worker that died on an unexpected error (the queue survives it)."""
    if worker.cancelled():
        return
    error = worker.exception()
    if error is not None:
        logger.error("Telegram ingestion worker crashed", exc_info=error)


def chat_id_from_user_id(user_id: str) -> int | None:
    """The private chat id behind a `tg:`-prefixed handle (None when malformed).

    Only for this surface's OWN records — the invite store and the member
    directory file people under `tg:<id>`, which is a Telegram id and always
    was. It must never be pointed at a core user id: those carry no structure,
    which is exactly what stops the delivery address from being read back out
    of who somebody is.
    """
    if not user_id.startswith(USER_ID_PREFIX):
        return None
    return _as_chat_id(user_id.removeprefix(USER_ID_PREFIX))


def _as_chat_id(external_id: str) -> int | None:
    """A Telegram account id as the chat to write to (private chats only)."""
    try:
        return int(external_id)
    except ValueError:
        return None


def _compose_voice_message(transcript: str, *, caption: str, origin: str, forwarded: bool) -> str:
    """Build the narrative text for a transcribed recording.

    Same shape as a described picture: the forward attribution first (when it
    is someone else's voice), the `[голосовое]` tag, then the words. A
    caption — Telegram allows one on an audio file — is appended verbatim.
    """
    text = f"{VOICE_TAG} {transcript}"
    if forwarded:
        attribution = (
            MATERIAL_ATTRIBUTION_TEMPLATE.format(origin=origin)
            if origin
            else MATERIAL_ATTRIBUTION_ANONYMOUS
        )
        text = f"{attribution} {text}"
    if caption:
        text = f"{text}\n\n{CAPTION_LABEL} {caption}"
    return text


def _declared_seconds(audio: TelegramAudioRef) -> int | None:
    """The recording's length as Telegram declared it; None when it did not.

    A voice note always carries one; an audio document may not, and inventing
    a number there would apply bounds to something nobody measured.
    """
    return None if audio.duration_seconds is None else int(audio.duration_seconds)


def _compose_images_message(
    descriptions: Sequence[MediaResult],
    *,
    caption: str,
    origin: str,
    forwarded: bool,
) -> str:
    """Build the narrative text for the described picture(s) of one message.

    Forwarded pictures get the same attribution prefix as
    `_dispatch_material` (`[переслано от X]`/anonymous); the user's own get
    none. Each description is tagged `[изображение]`, numbered when there is
    more than one (an album), and the caption — whichever item of an album
    carried it — is appended verbatim so it is never lost. A picture whose
    description failed keeps its slot: the agent must see that it was sent
    something it could not read, not silently lose a page.
    """
    total = len(descriptions)
    blocks = [
        f"{IMAGE_TAG if total == 1 else IMAGE_TAG_NUMBERED.format(index=index, total=total)} "
        f"{described.text if described.ok else IMAGE_FAILED_PLACEHOLDER}"
        for index, described in enumerate(descriptions, start=1)
    ]
    text = "\n\n".join(blocks)
    if forwarded:
        attribution = (
            MATERIAL_ATTRIBUTION_TEMPLATE.format(origin=origin)
            if origin
            else MATERIAL_ATTRIBUTION_ANONYMOUS
        )
        text = f"{attribution} {text}"
    if caption:
        text = f"{text}\n\n{CAPTION_LABEL} {caption}"
    return text
