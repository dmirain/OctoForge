"""In-memory draft book backed by optional cross-process persistence."""

import logging

from octoforge_telegram.bridge_state import Draft
from octoforge_telegram.drafts import DraftStore, PersistedDraft

logger = logging.getLogger(__name__)


class DraftBook:
    def __init__(self, user_id: str, chat_id: int, store: DraftStore | None) -> None:
        self._user_id = user_id
        self._chat_id = chat_id
        self._store = store
        self._drafts: dict[str | None, Draft] = {}

    async def restore(self) -> None:
        if self._store is None:
            return
        try:
            remembered = await self._store.load(self._chat_id)
        except Exception:
            logger.warning("could not load drafts for %s", self._user_id, exc_info=True)
            return
        for item in remembered:
            self._drafts.setdefault(
                item.exchange_id,
                Draft(
                    exchange_id=item.exchange_id,
                    message_id=item.message_id,
                    reply_to=item.reply_to,
                    sealed_chunks=item.sealed_chunks,
                ),
            )

    def get(self, exchange_id: str | None) -> Draft:
        draft = self._drafts.get(exchange_id)
        if draft is None:
            draft = Draft(exchange_id=exchange_id)
            self._drafts[exchange_id] = draft
        return draft

    def values(self) -> list[Draft]:
        return list(self._drafts.values())

    @property
    def entries(self) -> dict[str | None, Draft]:
        return self._drafts

    def pop(self, exchange_id: str | None) -> None:
        self._drafts.pop(exchange_id, None)

    async def remember(self, draft: Draft) -> None:
        if self._store is None or draft.exchange_id is None or draft.message_id is None:
            return
        try:
            await self._store.remember(
                self._chat_id,
                PersistedDraft(
                    draft.exchange_id,
                    draft.message_id,
                    draft.reply_to,
                    draft.sealed_chunks,
                ),
            )
        except Exception:
            logger.warning("could not remember draft for %s", self._user_id, exc_info=True)

    async def forget(self, exchange_id: str | None) -> None:
        if self._store is None or exchange_id is None:
            return
        try:
            await self._store.forget(exchange_id)
        except Exception:
            logger.warning("could not forget draft for %s", self._user_id, exc_info=True)
