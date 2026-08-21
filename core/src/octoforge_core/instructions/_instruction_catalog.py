"""Instruction lookup, publication, and deletion outcomes."""

from octoforge_core.instructions.ports import InstructionStore
from octoforge_core.instructions.types import (
    Instruction,
    InstructionNotFoundError,
    InstructionType,
    SystemInstructionError,
)

SYSTEM_RECORD_MESSAGE = (
    "'{title}' is a system instruction managed by the registry; it cannot be modified"
)


class InstructionCatalog:
    def __init__(self, store: InstructionStore) -> None:
        self._store = store

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None,
        user_id: str | None,
    ) -> Instruction:
        instruction = None
        if user_id is not None:
            instruction = await self._store.get_by_title(name, kind, owner_id=user_id)
        if instruction is None:
            instruction = await self._store.get_by_title(name, kind)
        if instruction is None:
            raise InstructionNotFoundError(name)
        return instruction

    async def memory_chars(self, owner_id: str) -> int:
        return await self._store.memory_chars(owner_id)

    async def list_system(self) -> list[Instruction]:
        return await self._store.list_system()

    async def list_public_by_prefix(self, kind: InstructionType, prefix: str) -> list[Instruction]:
        return await self._store.list_public_by_prefix(kind, prefix)

    async def delete(self, user_id: str, instruction_id: str) -> None:
        if not await self._store.delete_by_id(instruction_id, user_id):
            raise InstructionNotFoundError(instruction_id)

    async def publish(self, instruction_id: str) -> Instruction:
        instruction = await self._store.publish(instruction_id)
        if instruction is None:
            raise InstructionNotFoundError(instruction_id)
        return instruction

    async def delete_public(self, instruction_id: str) -> None:
        record = await self._store.get(instruction_id)
        if record is None or record.owner_id is not None:
            raise InstructionNotFoundError(instruction_id)
        if record.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=record.title))
        if not await self._store.delete_by_title(record.title, record.type):
            raise InstructionNotFoundError(instruction_id)

    async def delete_system(self, name: str, kind: InstructionType) -> None:
        if not await self._store.delete_by_title(name, kind):
            raise InstructionNotFoundError(name)
