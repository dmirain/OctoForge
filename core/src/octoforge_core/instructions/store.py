"""SQL adapter for the public instruction store port."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions._embedding_store import InstructionEmbeddingStore
from octoforge_core.instructions._instruction_commands import InstructionCommands
from octoforge_core.instructions._instruction_queries import InstructionQueries
from octoforge_core.instructions._instruction_rows import to_instruction
from octoforge_core.instructions._instruction_writer import InstructionWriter
from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionType,
)

__all__ = ["SqlAlchemyInstructionStore", "to_instruction"]


class SqlAlchemyInstructionStore:
    """Persist records, embeddings, counters, visibility, and publication."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._writer = InstructionWriter(session_factory)
        self._queries = InstructionQueries(session_factory)
        self._commands = InstructionCommands(session_factory)
        self._embeddings = InstructionEmbeddingStore(session_factory)

    async def upsert(self, draft: InstructionDraft) -> Instruction:
        return await self._writer.upsert(draft)

    async def get_by_title(
        self,
        title: str,
        kind: InstructionType | None,
        owner_id: str | None = None,
    ) -> Instruction | None:
        return await self._queries.get_by_title(title, kind, owner_id)

    async def get(self, instruction_id: str) -> Instruction | None:
        return await self._queries.get(instruction_id)

    async def list_with_embeddings(self, user_id: str | None) -> list[EmbeddedInstruction]:
        return await self._queries.list_with_embeddings(user_id)

    async def list_system(self) -> list[Instruction]:
        return await self._queries.list_system()

    async def list_public_by_prefix(self, kind: InstructionType, prefix: str) -> list[Instruction]:
        return await self._queries.list_public_by_prefix(kind, prefix)

    async def memory_chars(self, owner_id: str) -> int:
        return await self._queries.memory_chars(owner_id)

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        await self._commands.bump_usage(instruction_ids)

    async def delete_by_id(self, instruction_id: str, owner_id: str) -> bool:
        return await self._commands.delete_by_id(instruction_id, owner_id)

    async def publish(self, instruction_id: str) -> Instruction | None:
        return await self._commands.publish(instruction_id)

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        return await self._commands.delete_by_title(title, kind)

    async def list_stale_embeddings(self, model: str, limit: int) -> list[Instruction]:
        return await self._embeddings.list_stale(model, limit)

    async def count_stale_embeddings(self, model: str) -> int:
        return await self._embeddings.count_stale(model)

    async def set_embedding(
        self,
        instruction_id: str,
        embedding: tuple[float, ...],
        model: str,
    ) -> bool:
        return await self._embeddings.set(instruction_id, embedding, model)
