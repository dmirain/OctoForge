"""Replacement-aware plan limit for memory writes."""

from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)
from octoforge_core.memory.tool_contract import MEMORY_LIMIT_REFUSAL_TEMPLATE
from octoforge_core.tariffs.api import LimitGate


class MemoryQuota:
    """Decide whether one upsert fits after releasing replaced content."""

    def __init__(self, service: InstructionService, limits: LimitGate | None) -> None:
        self._service = service
        self._limits = limits

    async def refusal(self, user_id: str, key: str, content: str) -> str | None:
        if self._limits is None:
            return None
        cap = await self._limits.max_memory_chars(user_id)
        if cap is None:
            return None
        used = await self._service.memory_chars(user_id)
        replaced = await self._own_content_length(user_id, key)
        projected = used - replaced + len(content)
        if projected <= cap:
            return None
        return MEMORY_LIMIT_REFUSAL_TEMPLATE.format(projected=projected, limit=cap)

    async def _own_content_length(self, user_id: str, key: str) -> int:
        try:
            record = await self._service.get_by_name(key, InstructionType.MEMORY, user_id)
        except InstructionNotFoundError:
            return 0
        return len(record.content) if record.owner_id == user_id else 0
