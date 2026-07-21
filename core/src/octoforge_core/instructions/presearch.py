"""Skill pre-search: resolves the router's search queries into a branch note.

The LLMRouter emits free-text search queries for every incoming message; the
actor runs them through this adapter before the main run starts, so the
process branch begins with the relevant scenarios already in context (the
note is branch-only and never persisted into the narrative). Skill hits go in
with their full content (capped); knowledge/endpoint hits are listed as
one-line references the agent can pull with skills_search when needed.
"""

from octoforge_core.instructions.api import InstructionService, InstructionType, SearchHit

PER_QUERY_K = 2
MAX_SKILLS = 3
NOTE_HEADER = "Relevant scenarios for this run (follow them):"
REFERENCES_HEADER = "Related records (fetch with skills_search when needed):"


class InstructionPresearch:
    """PresearchPort over the InstructionService facade (merge, dedup, cap)."""

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    async def run(self, queries: tuple[str, ...]) -> str | None:
        """Search every query, then format the merged hits as a system note."""
        skills: dict[str, SearchHit] = {}
        references: dict[str, SearchHit] = {}
        for query in queries:
            for hit in await self._service.search(query, PER_QUERY_K):
                target = skills if hit.instruction.type is InstructionType.SKILL else references
                _keep_best(target, hit)
        if not skills and not references:
            return None
        return _format_note(skills, references)


def _keep_best(target: dict[str, SearchHit], hit: SearchHit) -> None:
    """Deduplicate by title, keeping the highest-scoring hit."""
    existing = target.get(hit.instruction.title)
    if existing is None or hit.score > existing.score:
        target[hit.instruction.title] = hit


def _by_score(hit: SearchHit) -> tuple[float, str]:
    return (-hit.score, hit.instruction.title)


def _format_note(skills: dict[str, SearchHit], references: dict[str, SearchHit]) -> str:
    lines = [NOTE_HEADER]
    for index, hit in enumerate(sorted(skills.values(), key=_by_score)[:MAX_SKILLS], start=1):
        lines.append(f"{index}. [skill] {hit.instruction.title}")
        lines.append(hit.instruction.content)
    if references:
        lines.append(REFERENCES_HEADER)
        for hit in sorted(references.values(), key=_by_score):
            lines.append(f"- [{hit.instruction.type.value}] {hit.instruction.title}")
    return "\n".join(lines)
