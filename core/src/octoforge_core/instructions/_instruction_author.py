"""Instruction authoring rules independent of storage and embedding backends."""

from dataclasses import dataclass

from octoforge_core.instructions._embedding_manager import InstructionEmbeddingManager
from octoforge_core.instructions.ports import InstructionStore
from octoforge_core.instructions.requests import InstructionDefinition
from octoforge_core.instructions.types import (
    Instruction,
    InstructionDraft,
    SystemInstructionError,
)

SYSTEM_RECORD_MESSAGE = (
    "'{title}' is a system instruction managed by the registry; it cannot be modified"
)


@dataclass(frozen=True, slots=True)
class _WriteScope:
    system: bool
    owner_id: str | None
    author_id: str | None
    lenient: bool


class InstructionAuthor:
    """Apply ownership, registry protection, and no-op update rules before upsert."""

    def __init__(
        self,
        store: InstructionStore,
        embeddings: InstructionEmbeddingManager,
    ) -> None:
        self._store = store
        self._embeddings = embeddings

    async def save(self, user_id: str, definition: InstructionDefinition) -> Instruction:
        published = await self._store.get_by_title(definition.title, definition.kind)
        if published is not None and published.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=definition.title))
        author_edit = published is not None and published.author_id == user_id
        return await self._write(
            definition,
            _WriteScope(False, None if author_edit else user_id, user_id, True),
        )

    async def save_public(self, definition: InstructionDefinition) -> Instruction:
        existing = await self._store.get_by_title(definition.title, definition.kind)
        if existing is not None:
            if existing.system:
                raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=definition.title))
            if _same_public(existing, definition):
                return existing
        return await self._write(definition, _WriteScope(False, None, None, True))

    async def save_system(self, definition: InstructionDefinition) -> Instruction:
        existing = await self._store.get_by_title(definition.title, definition.kind)
        if _same_system(existing, definition):
            assert existing is not None
            return existing
        return await self._write(definition, _WriteScope(True, None, None, False))

    async def _write(
        self,
        definition: InstructionDefinition,
        scope: _WriteScope,
    ) -> Instruction:
        embedding = (
            await self._embeddings.lenient(definition.title, definition.content)
            if scope.lenient
            else await self._embeddings.strict(definition.title, definition.content)
        )
        return await self._store.upsert(
            InstructionDraft(
                kind=definition.kind,
                title=definition.title,
                content=definition.content,
                tags=definition.tags,
                embedding=embedding,
                embedding_model=self._embeddings.model if embedding else None,
                system=scope.system,
                owner_id=scope.owner_id,
                author_id=scope.author_id,
            )
        )


def _same_public(existing: Instruction, definition: InstructionDefinition) -> bool:
    return (
        existing.owner_id is None
        and existing.content == definition.content
        and existing.tags == definition.tags
    )


def _same_system(
    existing: Instruction | None,
    definition: InstructionDefinition,
) -> bool:
    if existing is None or not existing.system:
        return False
    return existing.content == definition.content and existing.tags == definition.tags
