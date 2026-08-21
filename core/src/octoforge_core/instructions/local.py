"""Local in-process implementation of the instruction service port."""

from octoforge_core.instructions._embedding_manager import InstructionEmbeddingManager
from octoforge_core.instructions._instruction_author import InstructionAuthor
from octoforge_core.instructions._instruction_catalog import InstructionCatalog
from octoforge_core.instructions._instruction_search import InstructionSearchEngine
from octoforge_core.instructions.api import (
    Instruction,
    InstructionDefinition,
    InstructionSearchRequest,
    InstructionStore,
    InstructionType,
    SearchHit,
)
from octoforge_core.instructions.search_policy import (
    DEFAULT_RERANK_CANDIDATES,
    DEFAULT_SEARCH_OPTIONS,
    UNKNOWN_EMBEDDING_MODEL,
    InstructionSearchOptions,
    InstructionSearchPolicy,
)
from octoforge_core.llm.embeddings import EmbeddingClient

__all__ = [
    "DEFAULT_RERANK_CANDIDATES",
    "UNKNOWN_EMBEDDING_MODEL",
    "InstructionSearchOptions",
    "InstructionSearchPolicy",
    "LocalInstructionService",
]


class LocalInstructionService:
    """Search, author, publish, and maintain instructions over injected ports."""

    def __init__(
        self,
        store: InstructionStore,
        embedder: EmbeddingClient,
        options: InstructionSearchOptions = DEFAULT_SEARCH_OPTIONS,
    ) -> None:
        embeddings = InstructionEmbeddingManager(
            store,
            embedder,
            options.policy,
        )
        self._search = InstructionSearchEngine(
            store,
            embedder,
            options,
        )
        self._author = InstructionAuthor(store, embeddings)
        self._catalog = InstructionCatalog(store)
        self._embeddings = embeddings

    async def search(self, user_id: str, request: InstructionSearchRequest) -> list[SearchHit]:
        return await self._search.visible(user_id, request)

    async def search_all(self, request: InstructionSearchRequest) -> list[SearchHit]:
        return await self._search.all(request)

    async def save(self, user_id: str, definition: InstructionDefinition) -> Instruction:
        return await self._author.save(user_id, definition)

    async def save_public(self, definition: InstructionDefinition) -> Instruction:
        return await self._author.save_public(definition)

    async def save_system(self, definition: InstructionDefinition) -> Instruction:
        return await self._author.save_system(definition)

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        return await self._catalog.get_by_name(name, kind, user_id)

    async def memory_chars(self, owner_id: str) -> int:
        return await self._catalog.memory_chars(owner_id)

    async def list_system(self) -> list[Instruction]:
        return await self._catalog.list_system()

    async def list_public_by_prefix(self, kind: InstructionType, prefix: str) -> list[Instruction]:
        return await self._catalog.list_public_by_prefix(kind, prefix)

    async def delete(self, user_id: str, instruction_id: str) -> None:
        await self._catalog.delete(user_id, instruction_id)

    async def publish(self, instruction_id: str) -> Instruction:
        return await self._catalog.publish(instruction_id)

    async def delete_public(self, instruction_id: str) -> None:
        await self._catalog.delete_public(instruction_id)

    async def delete_system(self, name: str, kind: InstructionType) -> None:
        await self._catalog.delete_system(name, kind)

    async def resync_embeddings(self) -> int:
        return await self._embeddings.resync()
