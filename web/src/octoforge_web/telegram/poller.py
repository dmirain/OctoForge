"""Long-poll loop consuming Telegram updates and dispatching them to chat bridges."""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

import httpx
from octoforge_core.domain import MessageKind

from octoforge_web.telegram.bridge import RunnerProvider, TelegramBridge, TelegramBridgeOptions
from octoforge_web.telegram.client import (
    USER_ID_PREFIX,
    TelegramApiError,
    TelegramClient,
)
from octoforge_web.telegram.invites.api import (
    InviteAlreadyClaimedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteStatus,
    InviteStore,
    MemberDirectory,
)
from octoforge_web.telegram.models import (
    TelegramChatType,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)

logger = logging.getLogger(__name__)

COMMAND_START = "/start"
COMMAND_CANCEL = "/cancel"
COMMAND_SECRETS = "/secrets"
GREETING_TEXT = (
    "Привет! Я OctoForge — напиши вопрос, и я постараюсь помочь. "
    "Команда /cancel прерывает текущий ответ."
)
WELCOME_TEXT = (
    "Код принят, добро пожаловать! Я OctoForge — напиши вопрос, и я постараюсь помочь. "
    "Команда /cancel прерывает текущий ответ."
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
# how many recent album ids are remembered to collapse an album into one entry
ALBUM_MEMORY_SIZE = 64
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

    Admins (their numeric Telegram id in `admin_ids`) always pass. Anyone else
    needs an invite: either an already claimed one (status CLAIMED) or a fresh
    code passed as `/start <code>`, which is claimed atomically on entry.
    """

    def __init__(self, invite_store: InviteStore, admin_ids: Iterable[int]) -> None:
        self._invites = invite_store
        self._admin_ids = frozenset(admin_ids)

    async def check(self, user_id: str, text: str) -> MembershipDecision:
        """Decide whether the user may proceed; claims `/start <code>` invites."""
        numeric_id = chat_id_from_user_id(user_id)
        if numeric_id is not None and numeric_id in self._admin_ids:
            return MembershipDecision.ALLOW
        code = _start_code(text)
        if code is not None:
            return await self._claim(code, user_id)
        invite = await self._invites.get_by_user(user_id)
        if invite is not None and invite.status is InviteStatus.CLAIMED:
            return MembershipDecision.ALLOW
        return MembershipDecision.DENY_NO_ACCESS

    async def _claim(self, code: str, user_id: str) -> MembershipDecision:
        try:
            await self._invites.claim(code, user_id)
        except (InviteAlreadyClaimedError, InviteExpiredError, InviteNotFoundError):
            return MembershipDecision.DENY_INVITE_INVALID
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
        rich_messages_enabled: bool = True,
    ) -> None:
        self._runner_provider = runner_provider
        self._client = client
        self._options = TelegramBridgeOptions(
            edit_throttle_seconds=edit_throttle_seconds,
            rich_messages_enabled=rich_messages_enabled,
        )
        self._bridges: dict[str, TelegramBridge] = {}

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

    async def warm(self, user_ids: Iterable[str]) -> None:
        """Start bridges for known Telegram dialogs so their notifications are delivered."""
        for user_id in user_ids:
            chat_id = chat_id_from_user_id(user_id)
            if chat_id is None:
                continue
            await self.get_or_create(user_id, chat_id).start()

    async def aclose(self) -> None:
        """Stop all bridges (on app shutdown)."""
        for bridge in self._bridges.values():
            await bridge.aclose()


@dataclass(frozen=True, slots=True)
class TelegramPollerOptions:
    """Behavior knobs of the poller beyond its two collaborators.

    `secrets_link` builds the one-time secrets-form URL for a user id
    (None: the /secrets command reports the feature as not configured).
    """

    poll_timeout_seconds: float
    error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS
    membership: TelegramMembership | None = None
    secrets_link: Callable[[str], str] | None = None
    # who-is-who mirror: profiles of gated users, refreshed on every contact
    directory: MemberDirectory | None = None


class TelegramPoller:
    """Long-poll loop: fetch updates, advance the offset, dispatch to bridges."""

    def __init__(
        self,
        client: TelegramClient,
        registry: TelegramBridgeRegistry,
        options: TelegramPollerOptions,
    ) -> None:
        self._client = client
        self._registry = registry
        self._poll_timeout_seconds = options.poll_timeout_seconds
        self._error_backoff_seconds = options.error_backoff_seconds
        self._membership = options.membership
        self._secrets_link = options.secrets_link
        self._directory = options.directory
        self._offset: int | None = None
        # albums arrive as N updates sharing one media_group_id; only the
        # first becomes an entry (bounded, lost on restart — worst case an
        # album spanning a restart yields two entries)
        self._seen_albums: OrderedDict[str, None] = OrderedDict()

    async def run_forever(self) -> None:
        """Poll updates until cancelled.

        Three layers of defense, from innermost out: a `get_updates` failure
        only backs off and retries; a single update failing to dispatch is
        caught in `_dispatch_safely` and never stops the loop; this
        top-level catch-all is the last resort for whatever escapes both
        (an invites-store DB blip, a bug in code we didn't anticipate) — if
        it also killed the loop, the whole Telegram surface would go dark
        for every user until a process restart.
        """
        await self._drain_backlog()
        while True:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("Telegram poll loop hit an unexpected error; retrying")
                await asyncio.sleep(self._error_backoff_seconds)

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
        """Route one update: commands, group/non-text notices, forwards or plain text."""
        message = update.message
        if message is None or message.from_user is None:
            return
        chat_id = message.chat.id
        if message.chat.type is not TelegramChatType.PRIVATE:
            await self._client.send_message(chat_id, GROUP_NOTICE)
            return
        user_id = f"{USER_ID_PREFIX}{message.from_user.id}"
        # the gate comes before every reply: a stranger must not be able to
        # make the bot answer anything, not even the "text only" notice
        if not await self._check_membership(user_id, chat_id, message.body or ""):
            return
        # only past the gate: strangers knocking with bad codes stay unrecorded
        await self._record_member(user_id, message.from_user)
        if self._is_extra_album_item(message):
            return  # one entry per album, not one per photo
        if message.forward_origin is not None:
            await self._dispatch_material(message, user_id, chat_id)
            return
        if message.body is None:
            await self._client.send_message(chat_id, TEXT_ONLY_NOTICE)
            return
        reply_to_message_id = (
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        )
        await self._dispatch_text(
            message.message_id, user_id, chat_id, message.body, reply_to_message_id
        )

    def _is_extra_album_item(self, message: TelegramMessage) -> bool:
        """Whether this message is a follow-up item of an already-seen album."""
        group_id = message.media_group_id
        if group_id is None:
            return False
        if group_id in self._seen_albums:
            return True
        self._seen_albums[group_id] = None
        if len(self._seen_albums) > ALBUM_MEMORY_SIZE:
            self._seen_albums.popitem(last=False)
        return False

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
        bridge = self._registry.get_or_create(user_id=user_id, chat_id=chat_id)
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
        bridge = self._registry.get_or_create(user_id=user_id, chat_id=chat_id)
        if text == COMMAND_CANCEL:
            await bridge.cancel()
            return
        # the chat-level message id, not update_id: it doubles as the reply
        # target when the answer threads back to this question, and it is
        # unique per chat — enough for the (dialog, client_message_id) dedup
        await bridge.handle_text(
            text, client_message_id=str(message_id), reply_to_message_id=reply_to_message_id
        )

    async def _send_secrets_link(self, user_id: str, chat_id: int) -> None:
        if self._secrets_link is None:
            await self._client.send_message(chat_id, SECRETS_DISABLED_TEXT)
            return
        await self._client.send_message(
            chat_id, SECRETS_LINK_TEXT.format(url=self._secrets_link(user_id))
        )

    async def _record_member(self, user_id: str, user: TelegramUser) -> None:
        """Mirror the sender's profile; recording must never break dispatch."""
        if self._directory is None:
            return
        try:
            await self._directory.record(user_id, user.first_name, user.last_name, user.username)
        except Exception:
            logger.exception("member profile record failed: user=%s", user_id)

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


def chat_id_from_user_id(user_id: str) -> int | None:
    """Derive the private chat id from a tg-prefixed user id (None when malformed)."""
    if not user_id.startswith(USER_ID_PREFIX):
        return None
    try:
        return int(user_id.removeprefix(USER_ID_PREFIX))
    except ValueError:
        return None
