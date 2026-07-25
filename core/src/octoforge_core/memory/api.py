"""Public boundary of the memory module.

Everything the rest of the system (tools, executors) may know about memories
lives here: the `MemoryStore` protocol, the JSON-serializable DTO and the
module error.

The protocol is deliberately transport-shaped: the DTO contains only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation of `MemoryStore` is the planned "extract
to a dedicated service" path — call sites will not change.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class MemoryScope(StrEnum):
    """Visibility scope of a memory entry."""

    USER = "user"
    GLOBAL = "global"


class MemoryNotFoundError(Exception):
    """Raised when no memory matches the requested (owner, key) pair."""


@dataclass(frozen=True, slots=True)
class Memory:
    """One memory entry: owned by a user, or global when `user_id` is None.

    JSON-friendly: str fields, a tuple of tag strings and UTC datetimes
    (ISO 8601 at a wire boundary).
    """

    id: str
    user_id: str | None
    key: str
    content: str
    tags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class MemoryStore(Protocol):
    """Port of the memory module: put/get/search/delete keyed memories.

    Implementations: `SqlAlchemyMemoryStore` (SQL, in-process); a future HTTP
    client implementation for a dedicated memory service. The implementation
    is chosen in the composition root.
    """

    async def put(
        self,
        user_id: str | None,
        key: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> tuple[Memory, bool]:
        """Upsert by (user_id, key); return the memory and whether it was created.

        An existing entry of the same owner and key is replaced: content and
        tags are overwritten, updated_at is bumped, the id is kept. Otherwise
        a new entry is created. `user_id=None` is the global scope.
        """
        ...

    async def get(self, user_id: str | None, key: str) -> Memory:
        """Return the memory of this owner by key; raise `MemoryNotFoundError`."""
        ...

    async def search(self, user_id: str, query: str, limit: int) -> list[Memory]:
        """Return memories visible to this user matching a substring query.

        Visibility: the user's own entries plus the global ones
        (user_id IS NULL). Matching: case-insensitive substring over key OR
        content (SQL LIKE). Order: updated_at DESC, then key for determinism.
        A blank query is the catalog request: the newest `limit` visible
        memories with no filter at all. The agent cannot guess a substring for
        something it has never seen, so listing the whole (small) memory is the
        cheapest way for it to learn what it remembers about the user.
        """
        ...

    async def delete(self, user_id: str | None, key: str) -> None:
        """Delete the memory of this owner by key; raise `MemoryNotFoundError`."""
        ...
