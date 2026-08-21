"""The database home of a parked document: one record, the collections TTL.

Search and filtration are both database-level work, and both must survive a
task boundary — "поищи ещё в том же документе" arriving as the next message
must not force a refetch. So on Postgres a document too big for the context
lands here: a one-record collection (kind `doc_json`/`doc_text`) in the same
two tables, under the same TTL, quotas and sweeper as every other collection.
The RAM home (net/response_memory) remains the degradation for installations
without a database tier.
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionNotFoundError,
    CollectionStore,
    NewCollection,
    NewRecords,
)
from octoforge_core.net.collections.schema_infer import infer_records
from octoforge_core.net.response_memory import (
    REF_PREFIX,
    RENDER_IN_THREAD_CHARS,
    DocumentDraft,
    ResponseMemoryConfig,
    ResponseNotFoundError,
    StoredDocument,
    render_document_passport,
)
from octoforge_core.time import utc_now

#: The wrapper key of a text document's single record. Unambiguous against a
#: JSON document {"text": …} because the KIND tells them apart, not the shape.
TEXT_PAYLOAD_KEY = "text"


class DatabaseDocumentHome:
    """`DocumentHome` over the collections store."""

    def __init__(
        self,
        store: CollectionStore,
        config: CollectionConfig,
        memory_config: ResponseMemoryConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._memory_config = memory_config or ResponseMemoryConfig()

    async def park(self, draft: DocumentDraft) -> str:
        """Store the document as a one-record collection; answer its passport.

        `scope` is deliberately unused: lifetime here is the TTL, not the
        task — that is the point of this home.
        """
        # the DB home only ever receives a single JSON OBJECT or raw text:
        # arrays go to real collections, and the array-fallback exists only
        # where there is no database tier at all (the RAM home's case)
        parked_json = isinstance(draft.document, dict)
        kind = CollectionKind.DOC_JSON if parked_json else CollectionKind.DOC_TEXT
        payload: dict[str, Any]
        if isinstance(draft.document, dict):
            payload = draft.document
        else:
            payload = {TEXT_PAYLOAD_KEY: draft.body}
        passport = await self._store.create(
            NewCollection(
                owner_id=draft.owner_id,
                label="",
                kind=kind,
                source=draft.source,
                schema=infer_records([payload]),
                envelope={},
                records=NewRecords(payloads=[payload], source=draft.source),
                byte_size=len(draft.body),
                truncated=False,
                expires_at=self._expiry(),
            )
        )
        doc = StoredDocument(
            ref=f"{REF_PREFIX}{passport.id}",
            kind="json" if parked_json else "text",
            source=draft.source,
            body=draft.body,
            document=draft.document if parked_json else None,
        )
        minutes = max(1, int(self._config.ttl_seconds // 60))
        note = f"expires in {minutes} min"
        if len(draft.body) > RENDER_IN_THREAD_CHARS:
            return await asyncio.to_thread(render_document_passport, doc, self._memory_config, note)
        return render_document_passport(doc, self._memory_config, note)

    async def fetch(self, owner_id: str, ref: str) -> StoredDocument:
        """Read the document back; expiry and ownership answer not-found."""
        collection_id = ref.removeprefix(REF_PREFIX)
        try:
            passport = await self._store.passport(owner_id, collection_id)
            payload = await self._store.single_payload(owner_id, collection_id)
        except CollectionNotFoundError as gone:
            raise ResponseNotFoundError(ref) from gone
        if passport.kind is CollectionKind.DOC_TEXT:
            body = str(payload.get(TEXT_PAYLOAD_KEY, ""))
            return StoredDocument(
                ref=ref, kind="text", source=passport.source, body=body, document=None
            )
        if passport.byte_size > RENDER_IN_THREAD_CHARS:
            body = await asyncio.to_thread(json.dumps, payload, ensure_ascii=False)
        else:
            body = json.dumps(payload, ensure_ascii=False)
        return StoredDocument(
            ref=ref, kind="json", source=passport.source, body=body, document=payload
        )

    def _expiry(self) -> datetime:
        return utc_now() + timedelta(seconds=self._config.ttl_seconds)
