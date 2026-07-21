"""Tests for the context compactor: assembly, background compaction, safe cuts."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.runner import INTERRUPTED_NOTE
from octoforge_core.context.api import ArchivedMessage, DialogueSummary
from octoforge_core.context.compactor import (
    CompactorConfig,
    LlmContextCompactor,
    NoopContextCompactor,
    select_compact_segment,
)
from octoforge_core.context.prompts import parse_summary_reply
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.llm.events import StreamEvent
from octoforge_core.llm.usage import Completion, Usage
from octoforge_core.skills.base import SkillSpec
from octoforge_core.time import utc_now

USER_ID = "user-1"
CHANNEL = "web"
DAY = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.01
TEN_CHARS = "a" * 10
SUMMARY_REPLY = "TOPICS: alpha, beta\nSUMMARY:\ncompressed facts"
SECOND_SUMMARY_REPLY = "TOPICS: gamma\nSUMMARY:\nmore facts"
TWO_CALLS = 2
BLOCK_AND_TAIL = 2
THREE_MESSAGES = 3
FOUR_MESSAGES = 4
SIX_MESSAGES = 6


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # A file database, not :memory:: the in-memory StaticPool shares a single
    # connection, and a polling session closing mid-write of the background
    # compaction could roll back the compaction's uncommitted insert.
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemySummaryStore:
    return SqlAlchemySummaryStore(session_factory)


async def make_dialog(session_factory: async_sessionmaker[AsyncSession]) -> Dialog:
    return await DialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)


async def append_history(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
    texts: list[str],
) -> list[ChatMessage]:
    """Persist user messages (the actor's order: store first, narrative second)."""
    repository = MessageRepository(session_factory)
    history: list[ChatMessage] = []
    for text in texts:
        message = ChatMessage(role=MessageRole.USER, content=text)
        await repository.append(dialog_id, message)
        history.append(message)
    return history


def make_summary(dialog_id: str, seq_from: int, seq_to: int) -> DialogueSummary:
    return DialogueSummary(
        id=uuid.uuid4().hex,
        dialog_id=dialog_id,
        seq_from=seq_from,
        seq_to=seq_to,
        topics=("travel",),
        content="we planned a trip",
        created_at=utc_now(),
    )


def archived(seq: int, content: str, role: MessageRole = MessageRole.USER) -> ArchivedMessage:
    return ArchivedMessage(seq=seq, role=role, content=content, created_at=DAY)


class SummarizingLLM:
    """LLMClient stub replaying scripted summary replies via complete()."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        self.requests.append(list(messages))
        return Completion(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=self._replies.pop(0))
        )

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError


class GatedSummarizingLLM(SummarizingLLM):
    """SummarizingLLM pausing inside complete() until released."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(replies)
        self.calls = 0
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        self.calls += 1
        await self.release.wait()
        return await super().complete(messages, tools)


class FailingLLM(SummarizingLLM):
    """SummarizingLLM whose complete() always fails."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        raise RuntimeError("llm down")


def make_compactor(
    store: SqlAlchemySummaryStore,
    llm: SummarizingLLM,
    hot_max_chars: int = 100000,
    compact_target_chars: int = 50000,
) -> LlmContextCompactor:
    return LlmContextCompactor(
        store=store,
        archive=store,
        llm=llm,
        config=CompactorConfig(
            hot_max_chars=hot_max_chars,
            compact_target_chars=compact_target_chars,
        ),
    )


async def wait_for_condition(predicate: Callable[[], bool]) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_wait(), timeout=TIMEOUT_SECONDS)


# --- segment selection (safe cuts) -------------------------------------------


def test_segment_stops_between_messages_at_the_target() -> None:
    tail = [archived(1, TEN_CHARS), archived(2, TEN_CHARS), archived(3, TEN_CHARS)]

    segment = select_compact_segment(tail, target_chars=25)

    assert [m.seq for m in segment] == [1, 2]


def test_segment_takes_the_first_message_even_when_oversized() -> None:
    tail = [archived(1, "a" * 100), archived(2, TEN_CHARS)]

    segment = select_compact_segment(tail, target_chars=10)

    assert [m.seq for m in segment] == [1]
    assert select_compact_segment([], target_chars=10) == []


def test_segment_extends_past_a_split_salvaged_pair() -> None:
    tail = [
        archived(1, TEN_CHARS),
        archived(2, "partial", MessageRole.ASSISTANT),
        archived(3, INTERRUPTED_NOTE, MessageRole.SYSTEM),
        archived(4, "fresh"),
    ]

    segment = select_compact_segment(tail, target_chars=17)  # the cut lands inside the pair

    assert [m.seq for m in segment] == [1, 2, 3]  # both go into the summary


def test_segment_keeps_a_pair_whole_at_the_range_end() -> None:
    tail = [
        archived(1, "partial", MessageRole.ASSISTANT),
        archived(2, INTERRUPTED_NOTE, MessageRole.SYSTEM),
        archived(3, "fresh"),
    ]

    segment = select_compact_segment(tail, target_chars=200)

    assert [m.seq for m in segment] == [1, 2, 3]


def test_segment_may_cut_right_before_a_salvaged_pair() -> None:
    tail = [
        archived(1, TEN_CHARS),
        archived(2, "partial", MessageRole.ASSISTANT),
        archived(3, INTERRUPTED_NOTE, MessageRole.SYSTEM),
    ]

    segment = select_compact_segment(tail, target_chars=10)

    assert [m.seq for m in segment] == [1]  # the pair stays in the hot tail, together


# --- summary reply parsing ---------------------------------------------------


def test_parse_summary_reply_reads_topics_and_content() -> None:
    topics, content = parse_summary_reply(SUMMARY_REPLY)

    assert topics == ("alpha", "beta")
    assert content == "compressed facts"


def test_parse_summary_reply_tolerates_a_missing_format() -> None:
    topics, content = parse_summary_reply("just a plain summary")

    assert topics == ()
    assert content == "just a plain summary"


def test_parse_summary_reply_normalizes_and_caps_topics() -> None:
    topics, _ = parse_summary_reply("TOPICS: One, two, ONE, three, four, five\nSUMMARY:\nx")

    assert topics == ("one", "two", "three", "four")


# --- assemble -----------------------------------------------------------------


async def test_noop_compactor_returns_the_history_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    history = [ChatMessage(role=MessageRole.USER, content="hi")]
    compactor = NoopContextCompactor()

    assert await compactor.assemble(dialog, history) == history
    await compactor.aclose()


async def test_assemble_without_summaries_returns_the_full_tail(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([])
    compactor = make_compactor(store, llm)
    history = await append_history(session_factory, dialog.id, ["one", "two"])

    assert await compactor.assemble(dialog, history) == history
    assert llm.requests == []  # below the limit: no compaction triggered


async def test_assemble_prepends_the_topics_block_and_drops_compacted_messages(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    compactor = make_compactor(store, SummarizingLLM([]))
    history = await append_history(session_factory, dialog.id, ["old one", "old two", "new"])
    await store.create(make_summary(dialog.id, 1, 2))

    branch = await compactor.assemble(dialog, history)

    assert len(branch) == BLOCK_AND_TAIL  # topics block + the one tail message
    block = branch[0]
    assert block.role is MessageRole.SYSTEM
    assert "travel" in block.content
    assert "seq 1-2" in block.content
    assert "we planned a trip" in block.content
    assert branch[1].content == "new"
    assert all("old" not in message.content for message in branch)


# --- background compaction -----------------------------------------------------


async def test_overflow_compacts_the_oldest_messages_in_background(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([SUMMARY_REPLY])
    compactor = make_compactor(store, llm, hot_max_chars=20, compact_target_chars=25)
    history = await append_history(session_factory, dialog.id, [TEN_CHARS] * FOUR_MESSAGES)

    await compactor.assemble(dialog, history)  # 40 chars > 20: triggers compaction
    await wait_for_condition(lambda: len(llm.requests) == 1)
    segment_request = llm.requests[0]
    assert segment_request[0].role is MessageRole.SYSTEM
    assert "[1] user: " + TEN_CHARS in segment_request[1].content
    assert "[3] user" not in segment_request[1].content  # the fresh tail is untouched

    summaries = await _wait_for_summaries(store, dialog.id, count=1)
    (summary,) = summaries
    assert (summary.seq_from, summary.seq_to) == (1, 2)
    assert summary.topics == ("alpha", "beta")
    assert summary.content == "compressed facts"

    branch = await compactor.assemble(dialog, history)
    assert branch[0].role is MessageRole.SYSTEM
    assert "compressed facts" in branch[0].content
    assert [m.content for m in branch[1:]] == [TEN_CHARS, TEN_CHARS]


async def _wait_for_summaries(
    store: SqlAlchemySummaryStore, dialog_id: str, count: int
) -> list[DialogueSummary]:
    summaries: list[DialogueSummary] = []

    async def _wait() -> None:
        nonlocal summaries
        while True:
            summaries = await store.list_for_dialog(dialog_id)
            if len(summaries) >= count:
                return
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_wait(), timeout=TIMEOUT_SECONDS)
    return summaries


async def test_guard_runs_one_compaction_per_dialog_and_retriggers_after(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = GatedSummarizingLLM([SUMMARY_REPLY, SECOND_SUMMARY_REPLY])
    compactor = make_compactor(store, llm, hot_max_chars=15, compact_target_chars=25)
    history = await append_history(session_factory, dialog.id, [TEN_CHARS] * FOUR_MESSAGES)

    await compactor.assemble(dialog, history)
    await wait_for_condition(lambda: llm.calls == 1)
    await compactor.assemble(dialog, history)  # still over the limit, but gated
    await asyncio.sleep(0)
    assert llm.calls == 1  # the guard: no second run while the first is active

    llm.release.set()
    summaries = await _wait_for_summaries(store, dialog.id, count=1)
    assert (summaries[0].seq_from, summaries[0].seq_to) == (1, 2)

    await compactor.assemble(dialog, history)  # tail is still over: retrigger
    await wait_for_condition(lambda: llm.calls == TWO_CALLS)
    summaries = await _wait_for_summaries(store, dialog.id, count=TWO_CALLS)
    assert [(s.seq_from, s.seq_to) for s in summaries] == [(1, 2), (3, FOUR_MESSAGES)]


async def test_failed_compaction_logs_a_warning_and_the_dialog_lives(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    dialog = await make_dialog(session_factory)
    compactor = make_compactor(store, FailingLLM([]), hot_max_chars=20, compact_target_chars=25)
    history = await append_history(session_factory, dialog.id, [TEN_CHARS] * FOUR_MESSAGES)

    with caplog.at_level(logging.WARNING, logger="octoforge_core.context.compactor"):
        branch = await compactor.assemble(dialog, history)
        await wait_for_condition(
            lambda: any("context compaction failed" in r.message for r in caplog.records)
        )

    assert branch == history  # the dialog is unaffected by the failure
    assert await store.list_for_dialog(dialog.id) == []


async def test_repeated_compactions_cover_disjoint_ranges(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([SUMMARY_REPLY, SECOND_SUMMARY_REPLY])
    compactor = make_compactor(store, llm, hot_max_chars=25, compact_target_chars=25)
    history = await append_history(session_factory, dialog.id, [TEN_CHARS] * SIX_MESSAGES)

    await compactor.assemble(dialog, history)
    summaries = await _wait_for_summaries(store, dialog.id, count=1)
    assert (summaries[0].seq_from, summaries[0].seq_to) == (1, 2)

    await compactor.assemble(dialog, history)
    summaries = await _wait_for_summaries(store, dialog.id, count=TWO_CALLS)
    assert [(s.seq_from, s.seq_to) for s in summaries] == [(1, 2), (3, FOUR_MESSAGES)]

    await compactor.assemble(dialog, history)  # tail of 2 fits now: no third run
    await asyncio.sleep(0)
    assert len(llm.requests) == TWO_CALLS


# --- token-based trigger -------------------------------------------------------

MODEL_CONTEXT_TOKENS = 1000
CONTEXT_BUFFER_TOKENS = 100
OVERFLOW_PROMPT_TOKENS = 950
SMALL_PROMPT_TOKENS = 100
USAGE_COMPLETION_TOKENS = 5


def make_token_compactor(
    store: SqlAlchemySummaryStore,
    llm: SummarizingLLM,
    model_context_tokens: int = MODEL_CONTEXT_TOKENS,
) -> LlmContextCompactor:
    return LlmContextCompactor(
        store=store,
        archive=store,
        llm=llm,
        config=CompactorConfig(
            hot_max_chars=100000,  # the chars heuristic stays out of the way
            compact_target_chars=50000,
            model_context_tokens=model_context_tokens,
            context_buffer_tokens=CONTEXT_BUFFER_TOKENS,
        ),
    )


async def append_assistant_with_usage(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
    prompt_tokens: int,
) -> ChatMessage:
    message = ChatMessage(role=MessageRole.ASSISTANT, content="answer")
    await MessageRepository(session_factory).append(
        dialog_id,
        message,
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=USAGE_COMPLETION_TOKENS),
    )
    return message


async def test_token_overflow_triggers_compaction_without_char_overflow(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([SUMMARY_REPLY])
    compactor = make_token_compactor(store, llm)
    history = await append_history(session_factory, dialog.id, ["hello"])
    history.append(
        await append_assistant_with_usage(session_factory, dialog.id, OVERFLOW_PROMPT_TOKENS)
    )

    await compactor.assemble(dialog, history)

    await wait_for_condition(lambda: len(llm.requests) == 1)
    assert await _wait_for_summaries(store, dialog.id, count=1)


async def test_token_trigger_stays_quiet_below_threshold(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([SUMMARY_REPLY])
    compactor = make_token_compactor(store, llm)
    history = await append_history(session_factory, dialog.id, ["hello"])
    history.append(
        await append_assistant_with_usage(session_factory, dialog.id, SMALL_PROMPT_TOKENS)
    )

    await compactor.assemble(dialog, history)
    await asyncio.sleep(0)

    assert llm.requests == []


async def test_token_trigger_falls_back_to_chars_without_usage(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([SUMMARY_REPLY])
    compactor = make_token_compactor(store, llm)
    history = await append_history(session_factory, dialog.id, ["hello"])

    await compactor.assemble(dialog, history)  # no usage in the archive: no trigger
    await asyncio.sleep(0)

    assert llm.requests == []


async def test_token_trigger_disabled_with_zero_context(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = SummarizingLLM([SUMMARY_REPLY])
    compactor = make_token_compactor(store, llm, model_context_tokens=0)
    history = await append_history(session_factory, dialog.id, ["hello"])
    history.append(
        await append_assistant_with_usage(session_factory, dialog.id, OVERFLOW_PROMPT_TOKENS)
    )

    await compactor.assemble(dialog, history)
    await asyncio.sleep(0)

    assert llm.requests == []


async def test_latest_prompt_tokens_reads_the_newest_assistant_usage(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    assert await store.latest_prompt_tokens(dialog.id) is None

    await append_assistant_with_usage(session_factory, dialog.id, SMALL_PROMPT_TOKENS)
    await append_history(session_factory, dialog.id, ["user text"])
    await append_assistant_with_usage(session_factory, dialog.id, OVERFLOW_PROMPT_TOKENS)

    assert await store.latest_prompt_tokens(dialog.id) == OVERFLOW_PROMPT_TOKENS


async def test_aclose_cancels_a_pending_compaction(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await make_dialog(session_factory)
    llm = GatedSummarizingLLM([SUMMARY_REPLY])
    compactor = make_compactor(store, llm, hot_max_chars=20, compact_target_chars=25)
    history = await append_history(session_factory, dialog.id, [TEN_CHARS] * THREE_MESSAGES)

    await compactor.assemble(dialog, history)
    await wait_for_condition(lambda: llm.calls == 1)

    await compactor.aclose()
    llm.release.set()
    await asyncio.sleep(0)

    assert await store.list_for_dialog(dialog.id) == []  # the cancelled run wrote nothing
