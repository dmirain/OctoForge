"""Collections: schema inference, the spill decision, the store's lifecycle.

Everything here runs on SQLite because everything here is dialect-neutral —
inference is pure, the spill only parses and inserts, the store is plain
SQLAlchemy. The query engine is deliberately absent: it compiles to Postgres
jsonb SQL and lives in `test_postgres_stores.py`, behind `make test-pg`.
"""

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.composition import build_collections
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionNotFoundError,
    CollectionPassport,
    CollectionQuotaError,
    NewRecords,
    Query,
    QueryResult,
)
from octoforge_core.net.collections.ingest import ResponseSpill, render_passport
from octoforge_core.net.collections.schema_infer import (
    field_node,
    infer_records,
    known_fields,
    render,
)
from octoforge_core.net.collections.store import SqlAlchemyCollectionStore
from octoforge_core.net.collections.tools import CollectionQueryTool
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
OWNER = "person-1"
STRANGER = "person-2"
SOURCE = "endpoint:test"
LONG_ENOUGH = 3000  # over the default inline threshold of 2000
ITEM_COUNT = 100
CSV_ROWS = 300
FAT_BYTES = 6_000
TINY_CONFIG = CollectionConfig(max_per_user=2, max_bytes_per_user=10_000)
EXPECTED_PAGES = 2


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyCollectionStore:
    return SqlAlchemyCollectionStore(session_factory, TINY_CONFIG)


@pytest.fixture
def spill(store: SqlAlchemyCollectionStore) -> ResponseSpill:
    return ResponseSpill(store, TINY_CONFIG)


def _big_items(count: int = ITEM_COUNT) -> str:
    """A JSON body decidedly over the inline threshold."""
    return json.dumps(
        {
            "status": "ok",
            "items": [
                {"id": index, "name": f"contractor-{index:04d}", "amount": index * 1.5}
                for index in range(count)
            ],
        }
    )


# --- schema inference ---------------------------------------------------------


def test_schema_merges_optional_and_mixed_fields() -> None:
    """A field missing somewhere is optional; mixed scalar types degrade to string."""
    schema = infer_records(
        [
            {"id": 1, "name": "a", "extra": True},
            {"id": 2, "name": None},
            {"id": "oops"},
        ]
    )
    fields = schema["fields"]
    assert fields["id"]["type"] == "string"  # number + string = string
    assert fields["name"].get("nullable") is True
    assert fields["name"].get("optional") is True
    assert fields["extra"].get("optional") is True


def test_schema_render_names_the_shape() -> None:
    rendered = render(infer_records([{"id": 1, "tags": ["a"], "owner": {"city": "SPb"}}]))
    assert "id: number" in rendered
    assert "tags: array of string" in rendered
    assert "owner: {city: string}" in rendered


def test_field_paths_resolve_dotted_and_report_known() -> None:
    schema = infer_records([{"owner": {"city": "SPb"}, "id": 1}])
    node = field_node(schema, "owner.city")
    assert node is not None and node["type"] == "string"
    assert field_node(schema, "owner.street") is None
    assert known_fields(schema) == ["id", "owner"]


# --- the spill decision -------------------------------------------------------


async def test_a_small_body_stays_inline(spill: ResponseSpill) -> None:
    """Below the threshold the body itself is the best possible answer."""
    body = json.dumps({"items": [{"id": 1}]})
    assert await spill.spill(OWNER, body, "application/json", SOURCE, False) is None


async def test_unstructured_text_keeps_the_old_truncation(spill: ResponseSpill) -> None:
    assert await spill.spill(OWNER, "x" * LONG_ENOUGH, "text/html", SOURCE, False) is None


async def test_declared_json_that_does_not_parse_stays_inline(spill: ResponseSpill) -> None:
    body = "{" + "x" * LONG_ENOUGH
    assert await spill.spill(OWNER, body, "application/json", SOURCE, False) is None


async def test_a_big_json_body_becomes_a_collection_passport(
    spill: ResponseSpill, store: SqlAlchemyCollectionStore
) -> None:
    """The model sees the shape and the counts, never a truncated head."""
    passport_text = await spill.spill(OWNER, _big_items(), "application/json", SOURCE, False)

    assert passport_text is not None
    assert "100 records" in passport_text
    assert "amount: number" in passport_text
    assert '"status": "ok"' in passport_text  # the envelope rode along
    assert "col:" in passport_text
    ref = passport_text.split("col:", 1)[1].split("]", 1)[0]
    passport = await store.passport(OWNER, ref)
    assert passport.record_count == ITEM_COUNT
    assert passport.kind is CollectionKind.JSON


async def test_a_top_level_array_is_the_records(spill: ResponseSpill) -> None:
    body = json.dumps([{"id": index} for index in range(200)])
    passport_text = await spill.spill(OWNER, body, "", SOURCE, False)
    assert passport_text is not None and "200 records" in passport_text


async def test_csv_becomes_records_with_header_keys(
    spill: ResponseSpill, store: SqlAlchemyCollectionStore
) -> None:
    lines = ["name;amount"] + [f"contractor-{index};{index}" for index in range(CSV_ROWS)]
    passport_text = await spill.spill(OWNER, "\n".join(lines), "text/csv", SOURCE, False)

    assert passport_text is not None
    assert "kind=csv" in passport_text
    ref = passport_text.split("col:", 1)[1].split("]", 1)[0]
    passport = await store.passport(OWNER, ref)
    assert passport.record_count == CSV_ROWS
    # CSV values stay strings until a contract declares coercions
    assert passport.schema["fields"]["amount"]["type"] == "string"


async def test_a_spill_failure_falls_back_instead_of_failing_the_call(
    store: SqlAlchemyCollectionStore, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The data already arrived; losing it to a storage blip is not an option."""

    class ExplodingStore(SqlAlchemyCollectionStore):
        async def create(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("db down")

    broken = ResponseSpill(ExplodingStore(session_factory, TINY_CONFIG), TINY_CONFIG)
    assert await broken.spill(OWNER, _big_items(), "application/json", SOURCE, False) is None


# --- the store's lifecycle ----------------------------------------------------


async def test_the_owner_is_a_wall(store: SqlAlchemyCollectionStore) -> None:
    passport = await _create(store, OWNER)
    with pytest.raises(CollectionNotFoundError):
        await store.passport(STRANGER, passport.id)


async def test_expiry_reads_as_not_found(store: SqlAlchemyCollectionStore) -> None:
    """To the caller an expired collection never existed; the remedy is the same."""
    passport = await _create(store, OWNER, expires_in=timedelta(seconds=-1))
    with pytest.raises(CollectionNotFoundError):
        await store.passport(OWNER, passport.id)


async def test_the_sweeper_drops_expired_collections(store: SqlAlchemyCollectionStore) -> None:
    await _create(store, OWNER, expires_in=timedelta(seconds=-1))
    kept = await _create(store, OWNER)

    dropped = await store.delete_expired()

    assert dropped == 1
    assert (await store.passport(OWNER, kept.id)).id == kept.id


async def test_append_grows_and_reschemas(store: SqlAlchemyCollectionStore) -> None:
    passport = await _create(store, OWNER)
    grown = await store.append(
        OWNER,
        passport.id,
        NewRecords(payloads=[{"id": 3, "fresh": True}], source="endpoint:other"),
        schema=infer_records([{"id": 3, "fresh": True}]),
        byte_size=50,
        expires_at=utc_now() + timedelta(hours=1),
    )
    assert grown.record_count == passport.record_count + 1
    assert grown.pages_loaded == EXPECTED_PAGES
    assert "fresh" in grown.schema["fields"]


async def test_the_count_quota_evicts_the_least_recently_touched(
    store: SqlAlchemyCollectionStore,
) -> None:
    """max_per_user=2: the third collection pushes out the oldest, silently."""
    first = await _create(store, OWNER)
    second = await _create(store, OWNER)
    third = await _create(store, OWNER)

    with pytest.raises(CollectionNotFoundError):
        await store.passport(OWNER, first.id)
    assert (await store.passport(OWNER, second.id)).id == second.id
    assert (await store.passport(OWNER, third.id)).id == third.id


async def test_the_byte_quota_evicts_until_the_newcomer_fits(
    store: SqlAlchemyCollectionStore,
) -> None:
    """max_bytes=10k: one 6k tenant leaves when another 6k one arrives."""
    fat = await _create(store, OWNER, byte_size=FAT_BYTES)
    newcomer = await _create(store, OWNER, byte_size=FAT_BYTES)

    with pytest.raises(CollectionNotFoundError):
        await store.passport(OWNER, fat.id)
    assert (await store.passport(OWNER, newcomer.id)).byte_size == FAT_BYTES


async def test_the_passport_renders_expiry_and_truncation(
    store: SqlAlchemyCollectionStore,
) -> None:
    passport = await _create(store, OWNER, truncated=True)
    rendered = render_passport(passport)
    assert "SOURCE CUT" in rendered
    assert "expires in" in rendered
    assert "collection_query" in rendered


async def _create(
    store: SqlAlchemyCollectionStore,
    owner: str,
    expires_in: timedelta = timedelta(hours=1),
    byte_size: int = 100,
    truncated: bool = False,
) -> CollectionPassport:
    payloads = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    return await store.create(
        owner_id=owner,
        label="",
        kind=CollectionKind.JSON,
        source=SOURCE,
        schema=infer_records(payloads),
        envelope={},
        records=NewRecords(payloads=payloads, source=SOURCE),
        byte_size=byte_size,
        truncated=truncated,
        expires_at=utc_now() + expires_in,
    )


# --- composition: a Postgres-only capability ------------------------------------


async def test_the_feature_is_absent_off_postgres() -> None:
    """On SQLite `build_collections` answers None: no tools, no spill, and the
    call sites keep their truncation — the honest degradation is absence."""
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    assert build_collections(engine, create_session_factory(engine), CollectionConfig()) is None
    await engine.dispose()


async def test_append_refuses_past_the_byte_quota(store: SqlAlchemyCollectionStore) -> None:
    """The quota holds on BOTH write paths: create evicts, append refuses —
    a growing collection must not evict others to feed its own growth."""
    passport = await _create(store, OWNER, byte_size=9_000)

    with pytest.raises(CollectionQuotaError):
        await store.append(
            OWNER,
            passport.id,
            NewRecords(payloads=[{"id": 99}]),
            schema=infer_records([{"id": 99}]),
            byte_size=2_000,
            expires_at=utc_now() + timedelta(hours=1),
        )
    # what was there before the refusal is untouched
    untouched = 2
    assert (await store.passport(OWNER, passport.id)).record_count == untouched


async def test_query_result_size_is_the_models_choice() -> None:
    """The default is conservative; a deliberate max_chars takes more, the
    config ceiling holds whatever is asked."""

    class CannedEngine:
        async def execute(self, owner_id: str, ref: str, query: Query) -> QueryResult:
            return QueryResult(rows=[{"text": "x" * 300} for _ in range(10)], total=10)

    config = CollectionConfig(query_default_chars=200, query_max_chars=1000)
    tool = CollectionQueryTool(CannedEngine(), config)
    context = ToolContext(user_id=OWNER, channel="web", dialog_id="dialog-1")

    modest = await tool.execute({"ref": "col:x", "op": "get"}, context)
    deliberate = await tool.execute({"ref": "col:x", "op": "get", "max_chars": 900}, context)
    greedy = await tool.execute({"ref": "col:x", "op": "get", "max_chars": 90_000}, context)

    modest_bound, ceiling_bound = 500, 1500
    assert "raise max_chars" in modest and len(modest) < modest_bound
    assert len(deliberate) > len(modest)
    assert len(greedy) < ceiling_bound  # the ceiling held
