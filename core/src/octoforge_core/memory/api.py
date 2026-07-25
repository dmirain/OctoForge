"""Public boundary of the memory module.

Memory storage merged into the instruction store (`InstructionType.MEMORY`):
a memory is a private knowledge record whose title is the memory key, sharing
the table, the embeddings and the search machinery. What remains here is the
read-model DTO — the admin console lists memories in their own shape (key
instead of title, owner instead of record metadata) — and the intent-shaped
agent tools in `tools.py`.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Memory:
    """One memory entry: owned by a user, or a legacy global one (`user_id` None).

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
