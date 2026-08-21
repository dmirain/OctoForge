"""BM25 candidate retrieval over separate title and content indexes."""

import asyncio

import sqlalchemy as sa
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions._postgres_candidates import fetch_candidates, visible
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.ranking import fuse_rankings
from octoforge_core.instructions.requests import InstructionTextQuery
from octoforge_core.instructions.types import EmbeddedInstruction

CONTENT_BM25_INDEX = "ix_instructions_bm25_content"
TITLE_BM25_INDEX = "ix_instructions_bm25_title"
NO_MATCH_SCORE = 0


async def search_text(
    session_factory: async_sessionmaker[AsyncSession],
    request: InstructionTextQuery,
) -> list[EmbeddedInstruction]:
    """Fuse independent title and content BM25 rankings."""
    if request.limit <= 0 or not request.text.strip():
        return []
    rankings = await asyncio.gather(
        *(
            fetch_candidates(
                session_factory,
                visible(statement, request.user_id, request.kinds).limit(request.limit),
            )
            for statement in (
                _ranked_by(InstructionRow.content, CONTENT_BM25_INDEX, request.text),
                _ranked_by(InstructionRow.title, TITLE_BM25_INDEX, request.text),
            )
        )
    )
    return fuse_rankings(rankings)[: request.limit]


def _ranked_by(
    column: sa.orm.Mapped[str],
    index_name: str,
    query: str,
) -> Select[tuple[InstructionRow]]:
    relevance = column.op("<@>")(sa.func.to_bm25query(query, index_name))
    return select(InstructionRow).where(relevance < NO_MATCH_SCORE).order_by(relevance)
