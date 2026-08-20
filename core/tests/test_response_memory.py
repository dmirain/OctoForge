"""Task memory: the store, the passport's numbers, and the three verbs."""

import json

import pytest

from octoforge_core.composition import CollectionsRuntime, build_response_layer
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.net.collections.api import CollectionConfig
from octoforge_core.net.collections.documents import DatabaseDocumentHome
from octoforge_core.net.collections.engine import PostgresCollectionQueryEngine
from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.collections.store import SqlAlchemyCollectionStore
from octoforge_core.net.collections.tools import CollectionGetTool, CollectionQueryTool
from octoforge_core.net.response_memory import (
    ResponseFindTool,
    ResponseGetTool,
    ResponseMemory,
    ResponseMemoryConfig,
    ResponseNotFoundError,
    ResponseWindowTool,
    estimate_tokens,
)
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

OWNER = "person-1"
STRANGER = "person-2"
SCOPE = "task-1"
OTHER_SCOPE = "task-2"
SOURCE = "endpoint:issue"
RUSSIAN = "привет мир " * 100  # 1100 chars of mostly Cyrillic
ENGLISH = "hello world " * 100
SMALL_CONFIG = ResponseMemoryConfig(budget_chars=3000, get_default_chars=100, get_max_chars=500)


def make_context() -> ToolContext:
    return ToolContext(user_id=OWNER, channel="web", dialog_id="dialog-1")


def store_json(memory: ResponseMemory, document: dict) -> str:  # type: ignore[type-arg]
    body = json.dumps(document, ensure_ascii=False)
    return memory.store(OWNER, SCOPE, "json", SOURCE, body, document=document).ref


# --- the token estimate -------------------------------------------------------


def test_russian_costs_more_tokens_per_char_than_english() -> None:
    """The whole reason a char cap starves Russian: same chars, more tokens."""
    russian_rate = estimate_tokens(RUSSIAN) / len(RUSSIAN)
    english_rate = estimate_tokens(ENGLISH) / len(ENGLISH)
    assert russian_rate > english_rate * 1.4


# --- the store ----------------------------------------------------------------


def test_owner_is_a_wall_and_unknown_is_not_found() -> None:
    memory = ResponseMemory()
    ref = store_json(memory, {"a": 1})
    with pytest.raises(ResponseNotFoundError):
        memory.get(STRANGER, ref)
    with pytest.raises(ResponseNotFoundError):
        memory.get(OWNER, "resp:ghost")


def test_drop_scope_forgets_one_task_only() -> None:
    memory = ResponseMemory()
    doomed = store_json(memory, {"a": 1})
    kept = memory.store(OWNER, OTHER_SCOPE, "text", SOURCE, "still here").ref

    memory.drop_scope(SCOPE)

    with pytest.raises(ResponseNotFoundError):
        memory.get(OWNER, doomed)
    assert memory.get(OWNER, kept).body == "still here"


def test_lru_eviction_keeps_the_budget_and_the_newcomer() -> None:
    """3000-char budget: the oldest 2000-char tenant leaves for the new one."""
    memory = ResponseMemory(SMALL_CONFIG)
    old = memory.store(OWNER, SCOPE, "text", SOURCE, "x" * 2000).ref
    fresh = memory.store(OWNER, SCOPE, "text", SOURCE, "y" * 2000).ref

    with pytest.raises(ResponseNotFoundError):
        memory.get(OWNER, old)
    assert memory.get(OWNER, fresh).body[0] == "y"


# --- the passport -------------------------------------------------------------


async def test_json_passport_gives_the_numbers_to_decide_with() -> None:
    """Sizes in chars AND tokens — what the model weighs against its budget."""
    memory = ResponseMemory()
    document = {"title": "short", "body": RUSSIAN, "comments": [1, 2], "user": {"id": 1}}
    body = json.dumps(document, ensure_ascii=False)

    passport = await memory.park(OWNER, SCOPE, SOURCE, body, document=document)

    assert "body: string(1100 chars)" in passport
    assert "comments: array[2]" in passport
    assert "large text values: body (1100 chars ~" in passport
    assert "tokens" in passport
    assert "resp:" in passport
    assert "response_find" in passport  # the way out for what no budget fits


async def test_text_passport_carries_a_preview() -> None:
    memory = ResponseMemory()
    passport = await memory.park(OWNER, SCOPE, SOURCE, "<html>" + "word " * 500)
    assert "kind=text" in passport
    assert "preview: <html>" in passport


# --- response_get -------------------------------------------------------------


async def test_get_reads_a_key_in_full_when_asked() -> None:
    memory = ResponseMemory(SMALL_CONFIG)
    ref = store_json(memory, {"body": "x" * 400, "id": 7})
    tool = ResponseGetTool(memory, SMALL_CONFIG)

    modest = await tool.execute({"ref": ref, "key": "body"}, make_context())
    deliberate = await tool.execute({"ref": ref, "key": "body", "max_chars": 400}, make_context())

    # the default is conservative and says how to get the rest
    assert modest.startswith("x" * 100) and "raise max_chars" in modest
    assert deliberate == "x" * 400


async def test_get_ceiling_holds_whatever_is_asked() -> None:
    memory = ResponseMemory(SMALL_CONFIG)  # ceiling 500
    ref = store_json(memory, {"body": "x" * 900})
    tool = ResponseGetTool(memory, SMALL_CONFIG)
    answer = await tool.execute({"ref": ref, "key": "body", "max_chars": 9000}, make_context())
    assert answer.startswith("x" * 500)
    assert "showing 500 of 900" in answer


async def test_get_unknown_key_lists_the_real_ones() -> None:
    memory = ResponseMemory()
    ref = store_json(memory, {"body": "text", "id": 1})
    tool = ResponseGetTool(memory)

    with pytest.raises(ToolArgumentsError, match="body, id"):
        await tool.execute({"ref": ref, "key": "content"}, make_context())


async def test_get_gone_ref_names_the_remedy() -> None:
    tool = ResponseGetTool(ResponseMemory())
    answer = await tool.execute({"ref": "resp:ghost"}, make_context())
    assert "run the call again" in answer


# --- response_find and response_window ----------------------------------------

HAYSTACK = ("padding " * 50) + "the needle sits here" + (" tail" * 50) + " needle again"


async def test_find_answers_positions_windows_and_the_full_count() -> None:
    memory = ResponseMemory()
    item = memory.store(OWNER, SCOPE, "text", SOURCE, HAYSTACK)
    tool = ResponseFindTool(memory)

    answer = await tool.execute(
        {"ref": item.ref, "pattern": "needle", "before": 10, "after": 10}, make_context()
    )

    assert "2 match(es)" in answer
    assert "[at=" in answer
    assert "needle sits here" in answer


async def test_find_paging_and_the_narrowing_hint() -> None:
    memory = ResponseMemory()
    item = memory.store(OWNER, SCOPE, "text", SOURCE, "spot " * 100)
    tool = ResponseFindTool(memory)

    first = await tool.execute(
        {"ref": item.ref, "pattern": "spot", "max_matches": 3}, make_context()
    )
    second = await tool.execute(
        {"ref": item.ref, "pattern": "spot", "max_matches": 3, "match_offset": 3},
        make_context(),
    )

    assert "100 match(es); showing 1-3" in first
    assert "narrow the pattern" in first
    assert "showing 4-6" in second


async def test_find_merges_overlapping_windows() -> None:
    """Dense matches must not duplicate the same text five times."""
    memory = ResponseMemory()
    item = memory.store(OWNER, SCOPE, "text", SOURCE, "aaa bbb aaa bbb aaa")
    tool = ResponseFindTool(memory)
    answer = await tool.execute(
        {"ref": item.ref, "pattern": "aaa", "before": 50, "after": 50}, make_context()
    )
    merged_copies = 2
    assert answer.count("bbb") == merged_copies  # one merged window, no overlapping repeats


async def test_find_takes_a_broken_regex_literally() -> None:
    memory = ResponseMemory()
    item = memory.store(OWNER, SCOPE, "text", SOURCE, "price is $10 (net")
    tool = ResponseFindTool(memory)
    answer = await tool.execute({"ref": item.ref, "pattern": "$10 (net"}, make_context())
    assert "1 match(es)" in answer


async def test_window_widens_a_found_spot() -> None:
    memory = ResponseMemory()
    item = memory.store(OWNER, SCOPE, "text", SOURCE, HAYSTACK)
    at = HAYSTACK.index("needle")
    tool = ResponseWindowTool(memory)

    answer = await tool.execute(
        {"ref": item.ref, "at": at, "before": 20, "after": 30}, make_context()
    )

    assert "needle sits here" in answer
    assert f"of {len(HAYSTACK)}]" in answer


# --- the spill routes by shape ------------------------------------------------


async def test_a_single_document_goes_to_memory_not_the_database() -> None:
    """Scenario one of the design: the issue body never touches the base."""
    memory = ResponseMemory()
    spill = ResponseSpill(None, CollectionConfig(), memory)
    body = json.dumps({"title": "issue", "body": "т" * 5000})

    passport = await spill.spill(OWNER, body, "application/json", SOURCE, False, scope=SCOPE)

    assert passport is not None and "resp:" in passport and "col:" not in passport
    assert "body: string(5000 chars)" in passport


async def test_unstructured_text_goes_to_memory_verbatim() -> None:
    """Scenario three's cousin: a fetched HTML page becomes searchable."""
    memory = ResponseMemory()
    spill = ResponseSpill(None, CollectionConfig(), memory)
    page = "<html><head></head><body>" + "content " * 1000

    passport = await spill.spill(OWNER, page, "text/html", SOURCE, False, scope=SCOPE)

    assert passport is not None and "kind=text" in passport


async def test_an_array_without_a_database_still_becomes_a_readable_document() -> None:
    memory = ResponseMemory()
    spill = ResponseSpill(None, CollectionConfig(), memory)
    body = json.dumps({"items": [{"id": i} for i in range(300)]})

    passport = await spill.spill(OWNER, body, "application/json", SOURCE, False, scope=SCOPE)

    assert passport is not None and "resp:" in passport
    assert "items: array[300]" in passport


async def test_without_any_tier_the_old_truncation_stays() -> None:
    spill = ResponseSpill(None, CollectionConfig(), None)
    body = json.dumps({"title": "x", "body": "y" * 5000})
    assert await spill.spill(OWNER, body, "application/json", SOURCE, False) is None


def test_the_wire_limit_is_two_megabytes_unless_raised_consciously() -> None:
    """An API answering more per call is an API to page, not to buffer; the
    operator raises OF_RESPONSE_MEMORY_MAX_MB for genuinely bigger documents."""
    default = ResponseSpill(None, CollectionConfig(), ResponseMemory())
    raised = ResponseSpill(
        None,
        CollectionConfig(),
        ResponseMemory(ResponseMemoryConfig(max_response_chars=8 * 1024 * 1024)),
    )
    assert default.wire_limit_bytes == 2 * 1024 * 1024
    assert raised.wire_limit_bytes == 8 * 1024 * 1024


async def test_find_refuses_a_runaway_pattern() -> None:
    """The pattern is model-written; a 512-char one is not a search, it is a
    mistake — and the regex engine must never see it."""
    memory = ResponseMemory()
    item = memory.store(OWNER, SCOPE, "text", SOURCE, "haystack")
    tool = ResponseFindTool(memory)

    with pytest.raises(ToolArgumentsError, match="distinctive fragment"):
        await tool.execute({"ref": item.ref, "pattern": "a" * 600}, make_context())


async def test_csv_without_a_database_is_remembered_as_text() -> None:
    """No parsed document means kind=text — the passport must not claim JSON
    keys it cannot answer for."""
    spill = ResponseSpill(None, CollectionConfig(), ResponseMemory())
    lines = ["name;amount"] + [f"row-{i};{i}" for i in range(400)]

    passport = await spill.spill(OWNER, "\n".join(lines), "text/csv", SOURCE, False, scope=SCOPE)

    assert passport is not None and "kind=text" in passport


# --- the database home: search survives the task boundary ----------------------


async def _sqlite_home() -> "tuple[DatabaseDocumentHome, ResponseMemory, object]":
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    store = SqlAlchemyCollectionStore(create_session_factory(engine))
    memory = ResponseMemory()
    return DatabaseDocumentHome(store, CollectionConfig(), memory.config), memory, engine


async def test_a_parked_document_survives_the_task_that_fetched_it() -> None:
    """The point of the database home: 'поищи ещё в том же документе' as the
    NEXT message must not force a refetch — the TTL governs, not the task."""
    home, memory, engine = await _sqlite_home()
    passport = await home.park(
        OWNER, SCOPE, SOURCE, json.dumps({"body": "т" * 3000}), document={"body": "т" * 3000}
    )
    ref = "resp:" + passport.split("resp:", 1)[1].split("]", 1)[0]

    memory.drop_scope(SCOPE)  # the task ended; the RAM sweep must not matter

    fetched = await home.fetch(OWNER, ref)
    assert fetched.kind == "json"
    assert fetched.document == {"body": "т" * 3000}
    assert "expires in" in passport  # lifetime is the TTL, not the task
    await engine.dispose()  # type: ignore[union-attr]


async def test_the_database_home_round_trips_text_verbatim() -> None:
    home, _, engine = await _sqlite_home()
    page = "<html>" + "content " * 500
    passport = await home.park(OWNER, SCOPE, SOURCE, page)
    ref = "resp:" + passport.split("resp:", 1)[1].split("]", 1)[0]

    fetched = await home.fetch(OWNER, ref)

    assert fetched.kind == "text" and fetched.body == page
    tool = ResponseFindTool(home)
    answer = await tool.execute({"ref": ref, "pattern": "content"}, make_context())
    assert "match(es)" in answer
    await engine.dispose()  # type: ignore[union-attr]


async def test_with_a_database_the_spill_parks_documents_there() -> None:
    """RAM is the degradation, not the default: with a store present, a single
    document lands in the database and the RAM memory stays empty."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    store = SqlAlchemyCollectionStore(create_session_factory(engine))
    memory = ResponseMemory()
    home = DatabaseDocumentHome(store, CollectionConfig(), memory.config)
    spill = ResponseSpill(store, CollectionConfig(), memory, documents=home)
    body = json.dumps({"title": "issue", "body": "т" * 5000})

    passport = await spill.spill(OWNER, body, "application/json", SOURCE, False, scope=SCOPE)

    assert passport is not None and "resp:" in passport
    ref = "resp:" + passport.split("resp:", 1)[1].split("]", 1)[0]
    assert (await home.fetch(OWNER, ref)).kind == "json"
    with pytest.raises(ResponseNotFoundError):
        memory.get(OWNER, ref)  # nothing was parked in RAM
    await engine.dispose()


async def test_build_response_layer_wires_the_database_home_when_collections_exist() -> None:
    """The wiring itself, through the composition builder — a hand-built home
    in a test once masked a layer that silently kept parking into RAM."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    store = SqlAlchemyCollectionStore(factory)
    runtime = CollectionsRuntime(
        store=store,
        query_tool=CollectionQueryTool(PostgresCollectionQueryEngine(factory)),
        get_tool=CollectionGetTool(store),
    )

    layer = build_response_layer(runtime, CollectionConfig())
    body = json.dumps({"title": "issue", "body": "т" * 5000})
    passport = await layer.spill.spill(OWNER, body, "application/json", SOURCE, False, scope=SCOPE)

    assert passport is not None and "resp:" in passport
    ref = "resp:" + passport.split("resp:", 1)[1].split("]", 1)[0]
    # the READING tools resolve the same ref: same home on both sides
    answer = await layer.get_tool.execute({"ref": ref, "key": "title"}, make_context())
    assert answer == "issue"
    with pytest.raises(ResponseNotFoundError):
        layer.memory.get(OWNER, ref)  # RAM stayed empty: the database home took it
    await engine.dispose()


async def test_build_response_layer_falls_back_to_ram_without_collections() -> None:
    layer = build_response_layer(None, CollectionConfig())
    body = json.dumps({"title": "issue", "body": "т" * 5000})
    passport = await layer.spill.spill(OWNER, body, "application/json", SOURCE, False, scope=SCOPE)

    assert passport is not None and "resp:" in passport
    assert "lives until this task ends" in passport  # the RAM home's lifetime
