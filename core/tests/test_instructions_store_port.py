"""Substitution tests for the InstructionStore port.

An in-memory store is injected into the unmodified LocalInstructionService,
proving the storage layer is replaceable without touching the core (the
modularity roadmap, P1): brute-force ranking flows through
`list_with_embeddings`, vector-capable stores get `search_by_vector` instead.
"""

import pytest

from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionNotFoundError,
    InstructionType,
)
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.time import utc_now

TITLE_ALPHA = "alpha fact"
TITLE_BETA = "beta fact"
TITLE_GAMMA = "gamma fact"
CONTENT = "content"
QUERY = "find something"
EXACT_QUERY = "BETA FACT"  # matches TITLE_BETA case-insensitively
FIRST_VERSION = 1
SECOND_VERSION = 2
USAGE_ONCE = 1
TWO_HITS = 2
THREE_RECORDS = 3

V_RIGHT = (1.0, 0.0)
V_UP = (0.0, 1.0)
V_QUERY = (0.9, 0.1)


class StubEmbedder:
    """Deterministic EmbeddingClient: exact text-to-vector mapping."""

    def __init__(self) -> None:
        self.vectors: dict[str, tuple[float, ...]] = {}

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.vectors[text] for text in texts)


class InMemoryInstructionStore:
    """InstructionStore implementation keeping records in a dict (no SQL)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], Instruction] = {}
        self.embeddings: dict[str, tuple[float, ...]] = {}
        self.list_calls = 0

    async def upsert(self, draft: InstructionDraft) -> Instruction:
        key = (draft.kind.value, draft.title)
        existing = self.records.get(key)
        now = utc_now()
        record = Instruction(
            id=existing.id if existing is not None else f"mem-{len(self.records)}",
            type=draft.kind,
            title=draft.title,
            content=draft.content,
            tags=draft.tags,
            version=existing.version + 1 if existing is not None else FIRST_VERSION,
            usage_count=existing.usage_count if existing is not None else 0,
            success_count=existing.success_count if existing is not None else 0,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            system=draft.system,
        )
        self.records[key] = record
        self.embeddings[record.id] = draft.embedding
        return record

    async def get_by_title(self, title: str, kind: InstructionType | None) -> Instruction | None:
        matches = [
            record
            for record in self.records.values()
            if record.title == title and (kind is None or record.type is kind)
        ]
        if not matches:
            return None
        return min(matches, key=lambda record: (record.created_at, record.id))

    async def list_with_embeddings(self) -> list[EmbeddedInstruction]:
        self.list_calls += 1
        return [
            EmbeddedInstruction(instruction=record, embedding=self.embeddings[record.id])
            for record in self.records.values()
        ]

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        for key, record in self.records.items():
            if record.id in instruction_ids:
                self.records[key] = Instruction(
                    id=record.id,
                    type=record.type,
                    title=record.title,
                    content=record.content,
                    tags=record.tags,
                    version=record.version,
                    usage_count=record.usage_count + 1,
                    success_count=record.success_count,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        return self.records.pop((kind.value, title), None) is not None

    async def list_system(self) -> list[Instruction]:
        return sorted(
            (record for record in self.records.values() if record.system),
            key=lambda record: (record.created_at, record.id),
        )


class VectorSearchStore(InMemoryInstructionStore):
    """Adds the InstructionVectorSearch capability: the store runs the search."""

    def __init__(self) -> None:
        super().__init__()
        self.vector_calls: list[tuple[tuple[float, ...], int]] = []

    async def search_by_vector(
        self,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[EmbeddedInstruction]:
        self.vector_calls.append((query_embedding, limit))
        candidates = [
            EmbeddedInstruction(instruction=record, embedding=self.embeddings[record.id])
            for record in self.records.values()
        ]
        return candidates[:limit]


def make_embedder() -> StubEmbedder:
    embedder = StubEmbedder()
    for title in (TITLE_ALPHA, TITLE_BETA, TITLE_GAMMA):
        embedder.vectors[f"{title}\n{CONTENT}"] = V_RIGHT if title != TITLE_GAMMA else V_UP
    embedder.vectors[QUERY] = V_QUERY
    embedder.vectors[EXACT_QUERY] = V_QUERY
    return embedder


async def save_three(service: LocalInstructionService) -> None:
    for title in (TITLE_ALPHA, TITLE_BETA, TITLE_GAMMA):
        await service.save(InstructionType.KNOWLEDGE, title, CONTENT)


async def test_service_runs_over_an_in_memory_store() -> None:
    store = InMemoryInstructionStore()
    service = LocalInstructionService(store, make_embedder())

    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT, ("mem",))
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT, ("mem2",))
    stored = await service.get_by_name(TITLE_ALPHA, InstructionType.KNOWLEDGE)

    assert stored.version == SECOND_VERSION
    assert stored.tags == ("mem2",)

    hits = await service.search(QUERY, k=1)
    assert [hit.instruction.title for hit in hits] == [TITLE_ALPHA]
    assert (await service.get_by_name(TITLE_ALPHA)).usage_count == USAGE_ONCE

    await service.delete(TITLE_ALPHA, InstructionType.KNOWLEDGE)
    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(TITLE_ALPHA, InstructionType.KNOWLEDGE)


async def test_vector_capable_store_receives_the_search() -> None:
    store = VectorSearchStore()
    service = LocalInstructionService(store, make_embedder())
    await save_three(service)

    hits = await service.search(QUERY, k=TWO_HITS)

    assert store.vector_calls == [(V_QUERY, TWO_HITS)]
    assert store.list_calls == 0  # the brute-force path was not used
    assert len(hits) == TWO_HITS


async def test_exact_title_boost_applies_over_store_candidates() -> None:
    store = VectorSearchStore()
    service = LocalInstructionService(store, make_embedder())
    await save_three(service)

    hits = await service.search(EXACT_QUERY, k=THREE_RECORDS)

    assert hits[0].instruction.title == TITLE_BETA
    assert hits[0].score > hits[1].score
