"""Out-of-band operator notices to a person's Telegram account.

The console activates a queued user; the person deserves to hear it without
polling the bot. The message goes straight through the client rather than
through their dialog: it is an operational fact about access, not a turn of
the conversation, and the dialog machinery must not spin up an actor just
to say "the door is open".
"""

import logging

from octoforge_core.identity.api import IdentityStore

from octoforge_telegram.client import TELEGRAM_CHANNEL, TelegramClient

logger = logging.getLogger(__name__)

ACCESS_GRANTED_TEXT = "Место освободилось — доступ открыт! Напиши вопрос, и я постараюсь помочь."


class TelegramActivationNotifier:
    """Tells a person, on Telegram, that the operator opened their access."""

    def __init__(self, client: TelegramClient, identities: IdentityStore) -> None:
        self._client = client
        self._identities = identities

    async def __call__(self, user_id: str) -> bool:
        """Deliver the notice; report whether it went out.

        False is normal, not an error: the person may have no active
        Telegram identity (activated before ever writing, or seated on
        another surface). A delivery failure is logged and swallowed — the
        activation itself already happened and must not look failed.
        """
        chat_id = await self._chat_of(user_id)
        if chat_id is None:
            return False
        try:
            await self._client.send_message(chat_id, ACCESS_GRANTED_TEXT)
        except Exception:
            logger.warning("activation notice failed for user %r", user_id, exc_info=True)
            return False
        return True

    async def _chat_of(self, user_id: str) -> int | None:
        for identity in await self._identities.identities_of(user_id):
            if identity.surface == TELEGRAM_CHANNEL and identity.active:
                try:
                    return int(identity.external_id)
                except ValueError:
                    logger.warning(
                        "telegram identity of %r carries a non-numeric external id", user_id
                    )
                    return None
        return None
