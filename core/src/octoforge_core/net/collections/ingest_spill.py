"""Shape-aware routing of oversized responses."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionStore,
    NewCollection,
)
from octoforge_core.net.collections.ingest_models import ParsedBody, SpillRequest
from octoforge_core.net.collections.ingest_parse import parse_structured
from octoforge_core.net.collections.ingest_passport import render_passport
from octoforge_core.net.collections.ingest_schema import infer_schema, shape_records
from octoforge_core.net.response_memory import DocumentDraft, DocumentHome, ResponseMemory
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)
DB_WIRE_LIMIT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResponseSpillOptions:
    """Dependencies and policy of a response spill router."""

    store: CollectionStore | None
    config: CollectionConfig
    memory: ResponseMemory | None = None
    documents: DocumentHome | None = None


class ResponseSpill:
    """Route oversized arrays to collections and documents to a document home."""

    def __init__(self, options: ResponseSpillOptions) -> None:
        self._options = options
        self._documents = options.documents if options.documents is not None else options.memory

    @property
    def inline_max_chars(self) -> int:
        return self._options.config.inline_max_chars

    @property
    def config(self) -> CollectionConfig:
        return self._options.config

    @property
    def store(self) -> CollectionStore | None:
        return self._options.store

    @property
    def wire_limit_bytes(self) -> int:
        if self._options.memory is not None:
            return self._options.memory.config.max_response_chars
        return DB_WIRE_LIMIT_BYTES

    async def spill(self, request: SpillRequest) -> str | None:
        """Answer a passport, or None so the caller uses its truncation fallback."""
        if len(request.body) <= self.inline_max_chars:
            return None
        try:
            return await self._route(request)
        except Exception:
            logger.exception("response spill failed; falling back to truncation")
            return None

    async def _route(self, request: SpillRequest) -> str | None:
        response = request.response
        items_path = response.items_path if response is not None else None
        parsed = await parse_structured(request.body, request.content_type, items_path)
        if parsed is None:
            return await self._park(request)
        if not parsed.records.payloads:
            return None
        parked = await self._park_single(request, parsed)
        if parked is not None:
            return parked
        if self.store is None:
            return await self._park(request, parsed.document)
        return await self._collect(request, parsed)

    async def _park_single(self, request: SpillRequest, parsed: ParsedBody) -> str | None:
        if not parsed.single_document:
            return None
        return await self._park(request, parsed.document)

    async def _park(self, request: SpillRequest, document: object = None) -> str | None:
        if self._documents is None:
            return None
        draft = DocumentDraft(
            request.owner_id, request.scope, request.source, request.body, document
        )
        return await self._documents.park(draft)

    async def _collect(self, request: SpillRequest, parsed: ParsedBody) -> str:
        records = parsed.records
        if request.response is not None and request.response.fields:
            records = shape_records(records, request.response.fields)
        store = self.store
        assert store is not None
        passport = await store.create(
            NewCollection(
                request.owner_id,
                request.label,
                parsed.kind,
                request.source,
                await infer_schema(records.payloads),
                parsed.envelope,
                records,
                len(request.body),
                request.wire_truncated or parsed.record_truncated,
                self.expiry(),
            )
        )
        return render_passport(passport)

    def expiry(self) -> datetime:
        return utc_now() + timedelta(seconds=self.config.ttl_seconds)
