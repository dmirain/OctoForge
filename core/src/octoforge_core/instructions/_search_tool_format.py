"""Bounded text rendering for mixed instruction and dataset search results."""

from octoforge_core.datasets.api import DatasetHit
from octoforge_core.instructions.types import SearchHit

DATASET_SNIPPET_CHARS = 300
MAX_OUTPUT_CHARS = 8000
NO_HITS_MESSAGE = "no matching instructions or datasets"
TRUNCATED_MESSAGE = (
    "... output truncated: {omitted} more hit(s) not shown — refine the query or lower k"
)


def render_search(hits: list[SearchHit], dataset_hits: list[DatasetHit], limit: int) -> str:
    bodies = [_instruction_body(hit) for hit in hits]
    bodies.extend(_dataset_body(hit) for hit in dataset_hits)
    if not bodies:
        return NO_HITS_MESSAGE
    return _render(bodies[:limit])


def _render(bodies: list[str]) -> str:
    lines: list[str] = []
    length = 0
    omitted = 0
    for index, body in enumerate(bodies, start=1):
        numbered = f"{index}. {body}"
        if lines and length + len(numbered) + 1 > MAX_OUTPUT_CHARS:
            omitted = len(bodies) - index + 1
            break
        lines.append(numbered)
        length += len(numbered) + 1
    if omitted:
        lines.append(TRUNCATED_MESSAGE.format(omitted=omitted))
    return "\n".join(lines)


def _instruction_body(hit: SearchHit) -> str:
    instruction = hit.instruction
    return "\n".join(
        [
            f"[{instruction.type.value}] {instruction.title}",
            f"   id: {instruction.id}",
            f"   tags: {', '.join(instruction.tags) if instruction.tags else '-'}",
            instruction.content,
        ]
    )


def _dataset_body(hit: DatasetHit) -> str:
    dataset = hit.dataset
    field_names = ", ".join(field.name for field in dataset.schema.fields)
    snippet = dataset.description.replace("\n", " ")[:DATASET_SNIPPET_CHARS]
    return "\n".join(
        [
            f"[dataset] {dataset.name}",
            f"   fields: {field_names or '-'}",
            f"   {snippet}",
        ]
    )
