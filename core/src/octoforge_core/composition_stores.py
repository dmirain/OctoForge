"""Database-capability-aware store composition."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.composition_types import LexicalBackend
from octoforge_core.context.pg_store import PostgresSummaryStore
from octoforge_core.context.sqlite_store import SqliteSummaryStore
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.datasets.api import DatasetStore
from octoforge_core.datasets.pg_store import PostgresDatasetStore
from octoforge_core.datasets.sqlite_store import SqliteDatasetStore
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.instructions.api import InstructionStore
from octoforge_core.instructions.pg_store import PostgresInstructionStore
from octoforge_core.instructions.sqlite_store import SqliteInstructionStore
from octoforge_core.instructions.store import SqlAlchemyInstructionStore


def build_summary_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lexical_search: LexicalBackend,
) -> SqlAlchemySummaryStore:
    """Pick the archive store with the strongest available lexical search."""
    if lexical_search is LexicalBackend.POSTGRES:
        return PostgresSummaryStore(session_factory)
    if lexical_search is LexicalBackend.SQLITE:
        return SqliteSummaryStore(session_factory)
    return SqlAlchemySummaryStore(session_factory)


def build_dataset_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lexical_search: LexicalBackend,
) -> DatasetStore:
    """Pick the dataset store with the strongest available lexical search."""
    if lexical_search is LexicalBackend.POSTGRES:
        return PostgresDatasetStore(session_factory)
    if lexical_search is LexicalBackend.SQLITE:
        return SqliteDatasetStore(session_factory)
    return SqlAlchemyDatasetStore(session_factory)


def build_instruction_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    vector_search: bool,
    lexical_search: LexicalBackend = LexicalBackend.NONE,
) -> InstructionStore:
    """Pick a store whose class advertises only capabilities the database has."""
    if vector_search or lexical_search is LexicalBackend.POSTGRES:
        return PostgresInstructionStore(session_factory)
    if lexical_search is LexicalBackend.SQLITE:
        return SqliteInstructionStore(session_factory)
    return SqlAlchemyInstructionStore(session_factory)
