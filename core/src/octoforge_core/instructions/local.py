"""Local in-process implementation of the InstructionService facade.

SQL storage + brute-force cosine ranking; chosen in the composition root and
replaceable (e.g. by an HTTP client of a dedicated instructions service)
without changes to call sites.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionType,
    SearchHit,
)
from octoforge_core.instructions.embedding import EmbeddingClient
from octoforge_core.instructions.ranking import rank
from octoforge_core.instructions.store import InstructionStore

EMBEDDED_TEXT_SEPARATOR = "\n"


class LocalInstructionService:
    """InstructionService over the module-owned SQL table and an embedder."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingClient,
    ) -> None:
        self._store = InstructionStore(session_factory)
        self._embedder = embedder

    async def search(self, query: str, k: int) -> list[SearchHit]:
        """Embed the query, rank all records by cosine and bump usage of the hits."""
        if not query.strip():
            return []
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._store.list_with_embeddings()
        hits = rank(candidates, query, query_embedding, k)
        await self._store.bump_usage(tuple(hit.instruction.id for hit in hits))
        return hits

    async def save(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Embed title + content and upsert the record (version bump on replace)."""
        (embedding,) = await self._embedder.embed((_embedded_text(title, content),))
        return await self._store.upsert(kind, title, content, tags, embedding)

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        """Return the record by title, optionally narrowed by type."""
        instruction = await self._store.get_by_title(name, kind)
        if instruction is None:
            raise InstructionNotFoundError(name)
        return instruction


def _embedded_text(title: str, content: str) -> str:
    return f"{title}{EMBEDDED_TEXT_SEPARATOR}{content}"
