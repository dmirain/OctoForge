"""Task-scoped RAM home and its per-owner LRU lifecycle."""

import asyncio
import uuid

from octoforge_core.net.response_models import (
    REF_PREFIX,
    RENDER_IN_THREAD_CHARS,
    DocumentDraft,
    ResponseMemoryConfig,
    ResponseNotFoundError,
    StoredDocument,
    StoredResponse,
)
from octoforge_core.net.response_passport import render_document_passport
from octoforge_core.time import utc_now

RAM_LIFETIME_NOTE = "lives until this task ends"


class ResponseMemory:
    """Process-wide task RAM with owner isolation and per-owner LRU eviction."""

    def __init__(self, config: ResponseMemoryConfig | None = None) -> None:
        self._config = config or ResponseMemoryConfig()
        self._items: dict[str, StoredResponse] = {}

    @property
    def config(self) -> ResponseMemoryConfig:
        return self._config

    def store(self, draft: DocumentDraft) -> StoredResponse:
        """Park one draft, evicting the owner's least recently touched items."""
        item = StoredResponse(
            id=uuid.uuid4().hex[:12],
            owner_id=draft.owner_id,
            scope=draft.scope,
            kind=draft.kind,
            source=draft.source,
            body=draft.body,
            document=draft.document,
            last_access=utc_now().timestamp(),
        )
        self._items[item.id] = item
        self._evict(draft.owner_id)
        return item

    def get(self, owner_id: str, ref: str) -> StoredResponse:
        """Return an owner-scoped response and refresh its LRU seat."""
        item = self._items.get(ref.removeprefix(REF_PREFIX))
        if item is None or item.owner_id != owner_id:
            raise ResponseNotFoundError(ref)
        item.last_access = utc_now().timestamp()
        return item

    async def park(self, draft: DocumentDraft) -> str:
        """Park a draft in RAM for its task's lifetime."""
        doc = _to_document(self.store(draft))
        render = render_document_passport
        if len(draft.body) > RENDER_IN_THREAD_CHARS:
            return await asyncio.to_thread(render, doc, self._config, RAM_LIFETIME_NOTE)
        return render(doc, self._config, RAM_LIFETIME_NOTE)

    async def fetch(self, owner_id: str, ref: str) -> StoredDocument:
        """Fetch a parked document and refresh its LRU seat."""
        return _to_document(self.get(owner_id, ref))

    def drop_scope(self, scope: str) -> None:
        """Forget every response of one terminated task."""
        doomed = [key for key, item in self._items.items() if item.scope == scope]
        for key in doomed:
            del self._items[key]

    def _evict(self, owner_id: str) -> None:
        mine = [item for item in self._items.values() if item.owner_id == owner_id]
        while sum(len(item.body) for item in mine) > self._config.budget_chars:
            if len(mine) == 1:
                return
            oldest = min(mine, key=lambda item: item.last_access)
            del self._items[oldest.id]
            mine.remove(oldest)


def _to_document(item: StoredResponse) -> StoredDocument:
    return StoredDocument(
        ref=item.ref, kind=item.kind, source=item.source, body=item.body, document=item.document
    )
