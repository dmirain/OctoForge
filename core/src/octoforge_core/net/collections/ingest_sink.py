"""Multi-batch collection ingestion."""

from dataclasses import dataclass
from typing import Any

from octoforge_core.net.collections.api import (
    CollectionAppend,
    CollectionPassport,
    NewCollection,
    NewRecords,
)
from octoforge_core.net.collections.ingest_models import ParsedBody
from octoforge_core.net.collections.ingest_schema import merge_schema
from octoforge_core.net.collections.ingest_spill import ResponseSpill


@dataclass(frozen=True, slots=True)
class CollectionSinkOptions:
    """Identity, provenance, and destination of a collection sink."""

    spill: ResponseSpill
    owner_id: str
    source: str
    label: str = ""
    into: str | None = None


@dataclass(frozen=True, slots=True)
class _SinkBatch:
    parsed: ParsedBody
    records: NewRecords
    schema: dict[str, Any]
    byte_size: int


class CollectionSink:
    """Accumulate batches into one collection, creating it on first use."""

    def __init__(self, options: CollectionSinkOptions) -> None:
        self._options = options
        self._collection_id = options.into
        self._started_from_existing = options.into is not None
        self._schema: dict[str, Any] | None = None
        self.record_count = 0

    async def add(self, parsed: ParsedBody, byte_size: int) -> None:
        """Store one batch under this sink's source tag."""
        batch = NewRecords(parsed.records.payloads, self._options.source)
        store = self._options.spill.store
        assert store is not None
        await self._load_schema()
        merged = await merge_schema(self._schema, batch.payloads)
        state = _SinkBatch(parsed, batch, merged, byte_size)
        if self._collection_id is None:
            passport = await store.create(self._new_collection(state))
            self._collection_id = passport.id
        else:
            await store.append(self._append(state))
        self._schema = merged
        self.record_count += len(batch.payloads)

    async def _load_schema(self) -> None:
        if self._schema is not None or not self._started_from_existing:
            return
        assert self._collection_id is not None
        store = self._options.spill.store
        assert store is not None
        existing = await store.passport(self._options.owner_id, self._collection_id)
        self._schema = existing.schema

    def _new_collection(self, state: _SinkBatch) -> NewCollection:
        options = self._options
        return NewCollection(
            options.owner_id,
            options.label,
            state.parsed.kind,
            options.source,
            state.schema,
            state.parsed.envelope,
            state.records,
            state.byte_size,
            False,
            options.spill.expiry(),
        )

    def _append(self, state: _SinkBatch) -> CollectionAppend:
        assert self._collection_id is not None
        options = self._options
        return CollectionAppend(
            options.owner_id,
            self._collection_id,
            state.records,
            state.schema,
            state.byte_size,
            options.spill.expiry(),
        )

    async def finish(self, truncated: bool) -> CollectionPassport | None:
        """Persist truncation and answer the final passport, if any."""
        if self._collection_id is None:
            return None
        store = self._options.spill.store
        assert store is not None
        if truncated:
            await store.mark_truncated(self._options.owner_id, self._collection_id)
        return await store.passport(self._options.owner_id, self._collection_id)
