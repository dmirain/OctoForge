"""Collection-store commands, failures and persistence port."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from octoforge_core.net.collections.collection_types import (
    CollectionKind,
    CollectionPassport,
    NewRecords,
)


class CollectionError(Exception):
    """Base of agent-readable collection failures."""


class CollectionNotFoundError(CollectionError):
    """The ref names no live collection owned by the caller."""


class CollectionQueryError(CollectionError):
    """The query does not fit the collection schema."""


class CollectionQuotaError(CollectionError):
    """An append would exceed the owner's byte quota."""


@dataclass(frozen=True, slots=True)
class NewCollection:
    owner_id: str
    label: str
    kind: CollectionKind
    source: str
    schema: dict[str, Any]
    envelope: dict[str, Any]
    records: NewRecords
    byte_size: int
    truncated: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionAppend:
    owner_id: str
    collection_id: str
    records: NewRecords
    schema: dict[str, Any]
    byte_size: int
    expires_at: datetime


class CollectionStore(Protocol):
    async def create(self, collection: NewCollection) -> CollectionPassport: ...

    async def append(self, batch: CollectionAppend) -> CollectionPassport: ...

    async def passport(self, owner_id: str, collection_id: str) -> CollectionPassport: ...

    async def mark_truncated(self, owner_id: str, collection_id: str) -> None: ...

    async def single_payload(self, owner_id: str, collection_id: str) -> dict[str, Any]: ...

    async def delete_expired(self) -> int: ...
