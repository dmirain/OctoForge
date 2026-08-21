"""Candidate retrieval from vector, lexical, and portable stores."""

import asyncio
from dataclasses import dataclass

from octoforge_core.instructions.ports import (
    InstructionLexicalSearch,
    InstructionStore,
    InstructionVectorSearch,
)
from octoforge_core.instructions.ranking import fuse, rank
from octoforge_core.instructions.requests import (
    InstructionFusionRequest,
    InstructionRankingRequest,
    InstructionTextQuery,
    InstructionVectorQuery,
)
from octoforge_core.instructions.types import EmbeddedInstruction, InstructionType, SearchHit


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    query: str
    embedding: tuple[float, ...]
    limit: int
    user_id: str | None
    kinds: tuple[InstructionType, ...]


class CandidateRetriever:
    """Hide capability detection and hybrid retrieval from search orchestration."""

    def __init__(self, store: InstructionStore) -> None:
        self._store = store

    async def shortlist(self, request: CandidateRequest) -> list[SearchHit]:
        vector_hits, lexical_hits = await asyncio.gather(
            self._vector(request),
            self._lexical(request),
        )
        if lexical_hits is None:
            return await asyncio.to_thread(
                rank,
                InstructionRankingRequest(
                    vector_hits,
                    request.query,
                    request.embedding,
                    request.limit,
                ),
            )
        return fuse(
            InstructionFusionRequest(
                [vector_hits, lexical_hits],
                request.query,
                request.limit,
            )
        )

    async def _vector(self, request: CandidateRequest) -> list[EmbeddedInstruction]:
        if isinstance(self._store, InstructionVectorSearch):
            return await self._store.search_by_vector(
                InstructionVectorQuery(
                    request.embedding,
                    request.limit,
                    request.user_id,
                    request.kinds,
                )
            )
        rows = await self._store.list_with_embeddings(request.user_id)
        return [row for row in rows if not request.kinds or row.instruction.type in request.kinds]

    async def _lexical(self, request: CandidateRequest) -> list[EmbeddedInstruction] | None:
        if not isinstance(self._store, InstructionLexicalSearch):
            return None
        return await self._store.search_by_text(
            InstructionTextQuery(
                request.query,
                request.limit,
                request.user_id,
                request.kinds,
            )
        )
