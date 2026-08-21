"""Cross-user instruction search and publication for Telegram admins."""

from typing import Any

from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionSearchRequest,
    InstructionService,
    SearchHit,
)

from octoforge_telegram.admin_contract import (
    INSTRUCTION_SNIPPET_CHARS,
    MAX_INSTRUCTION_RESULTS,
    PUBLISH_NOT_FOUND_MESSAGE,
)


class AdminInstructionActions:
    def __init__(self, instructions: InstructionService) -> None:
        self._instructions = instructions

    async def search(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "error: query is required"
        hits = await self._instructions.search_all(
            InstructionSearchRequest(query, MAX_INSTRUCTION_RESULTS)
        )
        if not hits:
            return "no instructions found"
        lines = [f"instructions matching {query!r} (all owners):"]
        lines.extend(_instruction_line(index, hit) for index, hit in enumerate(hits, start=1))
        return "\n".join(lines)

    async def publish(self, arguments: dict[str, Any]) -> str:
        instruction_id = str(arguments.get("id") or "").strip()
        if not instruction_id:
            return "error: id is required"
        try:
            instruction = await self._instructions.publish(instruction_id)
        except InstructionNotFoundError:
            return PUBLISH_NOT_FOUND_MESSAGE
        return f"published: [{instruction.type.value}] {instruction.title}"


def _instruction_line(index: int, hit: SearchHit) -> str:
    instruction = hit.instruction
    owner = instruction.owner_id or "public"
    author = f", author: {instruction.author_id}" if instruction.author_id else ""
    snippet = instruction.content.replace("\n", " ")[:INSTRUCTION_SNIPPET_CHARS]
    return (
        f"{index}. [{instruction.type.value}] {instruction.title}\n"
        f"   id: {instruction.id} - owner: {owner}{author}\n"
        f"   {snippet}"
    )
