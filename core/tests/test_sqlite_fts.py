"""FTS5 lexical search on SQLite: the embedded deployment's keyword half.

Two things these tests exist to hold in place. The mirrors must stay true as
rows change — external-content FTS5 keeps only an index and reads the text back
through the rowid, so a missing trigger means silently stale results rather
than an error. And the tokenizer's limits are asserted rather than described,
because they differ from Postgres and users feel the difference.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_core.db.engine import (
    bootstrap_schema,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.db.sqlite_fts import has_sqlite_fts, match_expression
from octoforge_core.instructions.api import (
    InstructionDraft,
    InstructionLexicalSearch,
    InstructionType,
)
from octoforge_core.instructions.sqlite_store import SqliteInstructionStore
from octoforge_core.instructions.store import SqlAlchemyInstructionStore

OWNER = "user-a"
OTHER = "user-b"
EMBEDDING = (1.0, 0.0)
LIMIT = 10


@pytest.fixture
async def session_factory(tmp_path: object) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A migrated (not create_all) database — the FTS mirrors come from the chain."""
    engine: AsyncEngine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/fts.db")  # type: ignore[str-bytes-safe]
    await bootstrap_schema(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def draft(title: str, content: str, owner_id: str | None = OWNER) -> InstructionDraft:
    return InstructionDraft(
        kind=InstructionType.KNOWLEDGE,
        title=title,
        content=content,
        tags=(),
        embedding=EMBEDDING,
        owner_id=owner_id,
    )


async def test_the_migration_creates_the_mirrors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        connection = await session.connection()
        assert await connection.run_sync(has_sqlite_fts) is True


async def test_create_all_does_not_create_them() -> None:
    """`init_db` must leave lexical search off, or the whole suite would quietly
    switch from brute-force ranking to the fusion path and stop covering it."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        await init_db(engine)
        async with engine.connect() as connection:
            assert await connection.run_sync(has_sqlite_fts) is False
    finally:
        await engine.dispose()


async def test_a_saved_record_becomes_searchable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqliteInstructionStore(session_factory)
    saved = await store.upsert(draft("billing", "При ошибке E_INVOICE_4021 повторите запрос"))

    hits = await store.search_by_text("E_INVOICE_4021", limit=LIMIT, user_id=OWNER)

    assert [hit.instruction.id for hit in hits] == [saved.id]


async def test_an_edited_record_stops_matching_its_old_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The update trigger, without which the mirror silently serves stale text."""
    store = SqliteInstructionStore(session_factory)
    await store.upsert(draft("billing", "уникальное слово корвалол"))

    await store.upsert(draft("billing", "совершенно другое содержимое"))

    assert await store.search_by_text("корвалол", limit=LIMIT, user_id=OWNER) == []


async def test_a_deleted_record_leaves_no_trace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqliteInstructionStore(session_factory)
    saved = await store.upsert(draft("billing", "уникальное слово корвалол"))

    await store.delete_by_id(saved.id, OWNER)

    assert await store.search_by_text("корвалол", limit=LIMIT, user_id=OWNER) == []


async def test_search_honours_visibility_and_kind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqliteInstructionStore(session_factory)
    mine = await store.upsert(draft("mine", "уникальное слово корвалол"))
    await store.upsert(draft("theirs", "уникальное слово корвалол", owner_id=OTHER))

    visible = await store.search_by_text("корвалол", limit=LIMIT, user_id=OWNER)
    wrong_kind = await store.search_by_text(
        "корвалол", limit=LIMIT, user_id=OWNER, kinds=(InstructionType.ENDPOINT,)
    )

    assert [hit.instruction.id for hit in visible] == [mine.id]
    assert wrong_kind == []


async def test_trigram_matches_a_stem_but_not_a_longer_inflection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The honest boundary of SQLite lexical search, asserted rather than described.

    Postgres stems, so "задача" finds "задачи". SQLite has no Russian stemmer;
    trigram matches substrings, which covers the stem direction and not the
    other one. Anyone changing the tokenizer will see exactly what they traded.
    """
    store = SqliteInstructionStore(session_factory)
    saved = await store.upsert(draft("tasks", "Агент создаёт отложенные задачи"))

    by_stem = await store.search_by_text("задач", limit=LIMIT, user_id=OWNER)
    by_longer_form = await store.search_by_text("задача", limit=LIMIT, user_id=OWNER)

    assert [hit.instruction.id for hit in by_stem] == [saved.id]
    assert by_longer_form == []


async def test_only_the_sqlite_store_claims_the_capability(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert isinstance(SqliteInstructionStore(session_factory), InstructionLexicalSearch)
    assert not isinstance(SqlAlchemyInstructionStore(session_factory), InstructionLexicalSearch)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("погода", '"погода"'),
        ("две штуки", '"две" OR "штуки"'),
        ("a b", None),  # every token below the trigram minimum
        ("", None),
        # raw, this is an FTS5 syntax error; quoted, AND is a literal word
        ("город* AND", '"город*" OR "AND"'),
        ('он сказал "нет"', '"сказал" OR """нет"""'),
    ],
)
def test_a_user_query_can_never_be_fts5_syntax(query: str, expected: str | None) -> None:
    """MATCH takes a query language, not a string: an unescaped `AND` or a stray
    quote is either an error thrown at the user or a different search."""
    assert match_expression(query) == expected
