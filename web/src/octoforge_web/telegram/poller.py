"""Long-poll loop consuming Telegram updates and dispatching them to chat bridges."""

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

import httpx

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
)
from octoforge_web.telegram.models import TelegramChatType, TelegramUpdate

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
        self._offset: int | None = None

    async def run_forever(self) -> None:
        """Poll updates until cancelled; transient failures only log and back off."""
        await self._drain_backlog()
        while True:
            try:
                updates = await self._client.get_updates(self._offset, self._poll_timeout_seconds)
            except (httpx.HTTPError, TelegramApiError):
                logger.warning("Telegram polling failed; retrying", exc_info=True)
                await asyncio.sleep(self._error_backoff_seconds)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                await self._dispatch_safely(update)

    async def dispatch(self, update: TelegramUpdate) -> None:
        """Route one update: commands, non-text/group notices, or text into the bridge."""
        message = update.message
        if message is None or message.from_user is None:
            return
        chat_id = message.chat.id
        if message.chat.type is not TelegramChatType.PRIVATE:
            await self._client.send_message(chat_id, GROUP_NOTICE)
            return
        if message.text is None:
            await self._client.send_message(chat_id, TEXT_ONLY_NOTICE)
            return
        user_id = f"{USER_ID_PREFIX}{message.from_user.id}"
        if not await self._check_membership(user_id, chat_id, message.text):
            return
        await self._dispatch_text(update, user_id, chat_id, message.text)

    async def _dispatch_text(
        self, update: TelegramUpdate, user_id: str, chat_id: int, text: str
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
        await bridge.handle_text(text, client_message_id=str(update.update_id))

    async def _send_secrets_link(self, user_id: str, chat_id: int) -> None:
        if self._secrets_link is None:
            await self._client.send_message(chat_id, SECRETS_DISABLED_TEXT)
            return
        await self._client.send_message(
            chat_id, SECRETS_LINK_TEXT.format(url=self._secrets_link(user_id))
        )

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
        try:
            await self.dispatch(update)
        except (httpx.HTTPError, TelegramApiError):
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
