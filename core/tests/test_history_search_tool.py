"""Tests for the history_search tool over the real SQL store."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.api import DialogueSummary
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.context.tools import NO_HITS_MESSAGE, HistorySearchLimits, HistorySearchTool
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.dialogs.store import SqlAlchemyDialogRepository
from octoforge_core.domain import MessageRole
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
CHANNEL = "web"
DAY_ONE = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
DAY_TWO = datetime(2026, 1, 11, 10, 0, tzinfo=UTC)
CREATED = datetime(2026, 1, 9, 12, 0, tzinfo=UTC)
DEFAULT_LIMIT = 2
MAX_LIMIT = 4
TWO_HITS = 2
CTX_A = ToolContext(user_id="user-a", channel=CHANNEL, dialog_id="")
CTX_B = ToolContext(user_id="user-b", channel=CHANNEL, dialog_id="")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemySummaryStore:
    return SqlAlchemySummaryStore(session_factory)


@pytest.fixture
def tool(store: SqlAlchemySummaryStore) -> HistorySearchTool:
    return HistorySearchTool(
        archive=store,
        summaries=store,
        limits=HistorySearchLimits(default=DEFAULT_LIMIT, maximum=MAX_LIMIT),
    )


@pytest.fixture
async def dialogs(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[ToolContext, ToolContext]:
    """Two dialogs with messages; the contexts carry the real dialog ids."""
    repository = SqlAlchemyDialogRepository(session_factory)
    dialog_a = await repository.get_or_create(CTX_A.user_id, CHANNEL)
    dialog_b = await repository.get_or_create(CTX_B.user_id, CHANNEL)
    await _add_message(
        session_factory,
        MessageFixture(dialog_a.id, 1, "we flew to Berlin", DAY_ONE),
    )
    await _add_message(
        session_factory,
        MessageFixture(dialog_a.id, 2, "hotel in Berlin booked", DAY_TWO),
    )
    await _add_message(session_factory, MessageFixture(dialog_a.id, 3, "unrelated note", DAY_TWO))
    await _add_message(
        session_factory,
        MessageFixture(dialog_b.id, 1, "Berlin from another dialog", DAY_ONE),
    )
    ctx_a = ToolContext(user_id=CTX_A.user_id, channel=CHANNEL, dialog_id=dialog_a.id)
    ctx_b = ToolContext(user_id=CTX_B.user_id, channel=CHANNEL, dialog_id=dialog_b.id)
    return ctx_a, ctx_b


@dataclass(frozen=True, slots=True)
class MessageFixture:
    dialog_id: str
    seq: int
    content: str
    created_at: datetime


async def _add_message(
    session_factory: async_sessionmaker[AsyncSession],
    message: MessageFixture,
) -> None:
    async with session_factory() as session:
        session.add(
            MessageRow(
                id=uuid.uuid4().hex,
                dialog_id=message.dialog_id,
                seq=message.seq,
                role=MessageRole.USER.value,
                content=message.content,
                created_at=message.created_at,
            )
        )
        await session.commit()


@dataclass(frozen=True, slots=True)
class SummaryFixture:
    dialog_id: str
    seq_from: int
    seq_to: int
    topics: tuple[str, ...]


async def _add_summary(
    store: SqlAlchemySummaryStore,
    summary: SummaryFixture,
) -> None:
    await store.create(
        DialogueSummary(
            id=uuid.uuid4().hex,
            dialog_id=summary.dialog_id,
            seq_from=summary.seq_from,
            seq_to=summary.seq_to,
            topics=summary.topics,
            content="compressed",
            created_at=CREATED,
        )
    )


def test_spec(tool: HistorySearchTool) -> None:
    assert tool.spec.name == "history_search"
    assert tool.spec.parameters_schema["required"] == ["query"]


async def test_substring_search_returns_dated_entries(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    result = await tool.execute({"query": "berlin"}, ctx_a)

    lines = result.splitlines()
    assert len(lines) == TWO_HITS
    assert lines[0].startswith("1. [2026-01-10 10:00] seq 1 user: we flew to Berlin")
    assert lines[1].startswith("2. [2026-01-11 10:00] seq 2 user: hotel in Berlin booked")


async def test_search_is_isolated_to_the_own_dialog(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    _, ctx_b = dialogs

    result = await tool.execute({"query": "berlin"}, ctx_b)

    assert "another dialog" in result
    assert "we flew" not in result


async def test_default_limit_applies_and_explicit_limit_is_validated(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    limited = await tool.execute({"query": "o"}, ctx_a)
    single = await tool.execute({"query": "o", "limit": 1}, ctx_a)

    assert len(limited.splitlines()) == DEFAULT_LIMIT
    assert single == "1. [2026-01-10 10:00] seq 1 user: we flew to Berlin"
    with pytest.raises(ToolArgumentsError, match="limit must be an integer"):
        await tool.execute({"query": "o", "limit": "3"}, ctx_a)
    with pytest.raises(ToolArgumentsError, match="limit must be between"):
        await tool.execute({"query": "o", "limit": 0}, ctx_a)
    with pytest.raises(ToolArgumentsError, match="limit must be between"):
        await tool.execute({"query": "o", "limit": MAX_LIMIT + 1}, ctx_a)


async def test_query_must_be_a_non_empty_string(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    with pytest.raises(ToolArgumentsError, match="query must be a non-empty string"):
        await tool.execute({"query": "  "}, ctx_a)
    with pytest.raises(ToolArgumentsError, match="query must be a non-empty string"):
        await tool.execute({}, ctx_a)


async def test_topic_filter_restricts_hits_to_summary_ranges(
    tool: HistorySearchTool,
    store: SqlAlchemySummaryStore,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs
    await _add_summary(store, SummaryFixture(ctx_a.dialog_id, 2, 3, ("booking",)))

    result = await tool.execute({"query": "berlin", "topic": "Booking"}, ctx_a)

    assert "hotel in Berlin booked" in result
    assert "we flew" not in result  # seq 1 is outside the summary range


async def test_unknown_topic_matches_nothing(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    assert await tool.execute({"query": "berlin", "topic": "nope"}, ctx_a) == NO_HITS_MESSAGE
    with pytest.raises(ToolArgumentsError, match="topic must be a non-empty string"):
        await tool.execute({"query": "berlin", "topic": " "}, ctx_a)


async def test_date_filters_bound_the_hits(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    from_hits = await tool.execute({"query": "berlin", "date_from": "2026-01-11"}, ctx_a)
    to_hits = await tool.execute({"query": "berlin", "date_to": "2026-01-10"}, ctx_a)

    assert "hotel in Berlin booked" in from_hits
    assert "we flew" not in from_hits
    assert "we flew" in to_hits
    assert "hotel" not in to_hits  # a date-only upper bound covers the whole day


async def test_datetime_date_to_is_an_exclusive_bound(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    exact = await tool.execute({"query": "berlin", "date_to": "2026-01-10T10:00:00"}, ctx_a)
    just_after = await tool.execute({"query": "berlin", "date_to": "2026-01-10T10:00:01"}, ctx_a)

    assert exact == NO_HITS_MESSAGE  # the message at exactly the bound is excluded
    assert "we flew" in just_after


async def test_invalid_dates_are_rejected(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    with pytest.raises(ToolArgumentsError, match="date_from must be an ISO"):
        await tool.execute({"query": "berlin", "date_from": "yesterday"}, ctx_a)
    with pytest.raises(ToolArgumentsError, match="date_to must be an ISO"):
        await tool.execute({"query": "berlin", "date_to": "10.01.2026"}, ctx_a)


async def test_no_hits_message(
    tool: HistorySearchTool,
    dialogs: tuple[ToolContext, ToolContext],
) -> None:
    ctx_a, _ = dialogs

    assert await tool.execute({"query": "timbuktu"}, ctx_a) == NO_HITS_MESSAGE
