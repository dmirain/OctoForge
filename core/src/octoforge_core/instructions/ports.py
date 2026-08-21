"""Public instruction service and persistence ports."""

from typing import Protocol, runtime_checkable

from octoforge_core.instructions.requests import (
    InstructionDefinition,
    InstructionSearchRequest,
    InstructionTextQuery,
    InstructionVectorQuery,
)
from octoforge_core.instructions.types import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionType,
    SearchHit,
)


class InstructionStore(Protocol):
    """Persistence port for instruction records and embeddings."""

    async def upsert(self, draft: InstructionDraft) -> Instruction: ...

    async def get_by_title(
        self, title: str, kind: InstructionType | None, owner_id: str | None = None
    ) -> Instruction | None: ...

    async def get(self, instruction_id: str) -> Instruction | None: ...

    async def list_with_embeddings(self, user_id: str | None) -> list[EmbeddedInstruction]: ...

    async def list_system(self) -> list[Instruction]: ...

    async def list_public_by_prefix(
        self, kind: InstructionType, prefix: str
    ) -> list[Instruction]: ...

    async def memory_chars(self, owner_id: str) -> int: ...

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None: ...

    async def delete_by_id(self, instruction_id: str, owner_id: str) -> bool: ...

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool: ...

    async def publish(self, instruction_id: str) -> Instruction | None: ...

    async def list_stale_embeddings(self, model: str, limit: int) -> list[Instruction]: ...

    async def set_embedding(
        self, instruction_id: str, embedding: tuple[float, ...], model: str
    ) -> bool: ...


@runtime_checkable
class InstructionVectorSearch(Protocol):
    """Optional storage-side vector ranking capability."""

    async def search_by_vector(
        self, request: InstructionVectorQuery
    ) -> list[EmbeddedInstruction]: ...


@runtime_checkable
class InstructionLexicalSearch(Protocol):
    """Optional storage-side lexical ranking capability."""

    async def search_by_text(self, request: InstructionTextQuery) -> list[EmbeddedInstruction]: ...


class InstructionService(Protocol):
    """Facade over instruction search, authoring, publication, and maintenance."""

    async def search(self, user_id: str, request: InstructionSearchRequest) -> list[SearchHit]: ...

    async def search_all(self, request: InstructionSearchRequest) -> list[SearchHit]: ...

    async def save(self, user_id: str, definition: InstructionDefinition) -> Instruction: ...

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction: ...

    async def memory_chars(self, owner_id: str) -> int: ...

    async def delete(self, user_id: str, instruction_id: str) -> None: ...

    async def delete_public(self, instruction_id: str) -> None: ...

    async def publish(self, instruction_id: str) -> Instruction: ...

    async def save_public(self, definition: InstructionDefinition) -> Instruction: ...

    async def list_public_by_prefix(
        self, kind: InstructionType, prefix: str
    ) -> list[Instruction]: ...

    async def save_system(self, definition: InstructionDefinition) -> Instruction: ...

    async def list_system(self) -> list[Instruction]: ...

    async def delete_system(self, name: str, kind: InstructionType) -> None: ...

    async def resync_embeddings(self) -> int: ...
