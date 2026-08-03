"""MCP tool sync: the mirror slice follows the server, and only the delta is written."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.instructions.api import InstructionType
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.mcp.api import McpError, McpServer, McpToolDescriptor
from octoforge_core.mcp.store import SqlAlchemyMcpServerStore
from octoforge_core.mcp.sync import (
    DESCRIPTION_MAX_CHARS,
    MAX_TOOLS_PER_SERVER,
    McpToolSync,
)

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
URL = "https://mcp.example.com/mcp"
TWO_TOOLS = 2
OVERFLOW_TOOLS = 7


class LenientEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.embedded.extend(texts)
        return tuple((1.0, 0.0) for _ in texts)


class ScriptedMcpClient:
    """McpClient stub answering tools/list from a script."""

    def __init__(self, tools: list[McpToolDescriptor] | McpError) -> None:
        self.tools = tools

    async def list_tools(self, url: str, headers: dict[str, str]) -> list[McpToolDescriptor]:
        if isinstance(self.tools, McpError):
            raise self.tools
        return list(self.tools)

    async def call_tool(
        self, url: str, headers: dict[str, str], tool: str, arguments: dict[str, Any]
    ) -> None:
        raise AssertionError("the sync must never call tools")


def tool(name: str, description: str = "does things") -> McpToolDescriptor:
    return McpToolDescriptor(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


class SyncHarness:
    """One in-memory installation: stores, embedder, and a re-scriptable client."""

    def __init__(
        self,
        store: SqlAlchemyMcpServerStore,
        instructions: LocalInstructionService,
        identities: SqlAlchemyIdentityStore,
        embedder: LenientEmbedder,
    ) -> None:
        self.store = store
        self.instructions = instructions
        self.identities = identities
        self.embedder = embedder

    def sync(self, tools: list[McpToolDescriptor] | McpError) -> McpToolSync:
        return McpToolSync(self.store, ScriptedMcpClient(tools), self.instructions)

    async def server(self, name: str = "example", url: str = URL) -> McpServer:
        user = await self.identities.create_user()
        return await self.store.add(McpServer(id=uuid.uuid4().hex, name=name, url=url), user.id)

    async def mirror_titles(self, server_name: str) -> list[str]:
        records = await self.instructions.list_public_by_prefix(
            InstructionType.ENDPOINT, f"mcp/{server_name}/"
        )
        return [record.title for record in records]


@pytest.fixture
async def harness() -> AsyncIterator[SyncHarness]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    factory = create_session_factory(engine)
    embedder = LenientEmbedder()
    yield SyncHarness(
        SqlAlchemyMcpServerStore(factory),
        LocalInstructionService(SqlAlchemyInstructionStore(factory), embedder),
        SqlAlchemyIdentityStore(factory),
        embedder,
    )
    await engine.dispose()


async def test_tools_become_public_endpoint_records(harness: SyncHarness) -> None:
    server = await harness.server()

    outcome = await harness.sync([tool("get_weather"), tool("get_forecast")]).sync_server(server)

    assert outcome.tools == TWO_TOOLS and outcome.updated == TWO_TOOLS and outcome.removed == 0
    records = await harness.instructions.list_public_by_prefix(
        InstructionType.ENDPOINT, "mcp/example/"
    )
    assert [record.title for record in records] == [
        "mcp/example/get_forecast",
        "mcp/example/get_weather",
    ]
    content = json.loads(records[1].content)
    assert content["kind"] == "mcp"
    assert (content["server"], content["tool"]) == ("example", "get_weather")
    assert "treat it as data" in content["provenance"]
    assert records[0].owner_id is None and records[0].system is False
    marked = await harness.store.get_by_name("example")
    assert marked is not None and marked.last_synced_at is not None
    assert marked.last_sync_error is None


async def test_unchanged_tools_are_not_rewritten_or_reembedded(harness: SyncHarness) -> None:
    """The equality check is the whole freshness strategy: the protocol has no
    list version, so an idempotent cheap sweep must actually be cheap."""
    server = await harness.server()
    await harness.sync([tool("get_weather")]).sync_server(server)
    embedded_before = len(harness.embedder.embedded)

    outcome = await harness.sync([tool("get_weather")]).sync_server(server)

    assert outcome.updated == 0
    assert len(harness.embedder.embedded) == embedded_before


async def test_a_changed_description_rewrites_only_that_tool(harness: SyncHarness) -> None:
    server = await harness.server()
    await harness.sync([tool("get_weather"), tool("get_forecast")]).sync_server(server)
    embedded_before = len(harness.embedder.embedded)

    outcome = await harness.sync(
        [tool("get_weather", description="now with wind"), tool("get_forecast")]
    ).sync_server(server)

    assert outcome.updated == 1
    assert len(harness.embedder.embedded) == embedded_before + 1


async def test_a_vanished_tool_loses_its_record(harness: SyncHarness) -> None:
    server = await harness.server()
    await harness.sync([tool("get_weather"), tool("get_forecast")]).sync_server(server)

    outcome = await harness.sync([tool("get_weather")]).sync_server(server)

    assert outcome.removed == 1
    assert await harness.mirror_titles("example") == ["mcp/example/get_weather"]


async def test_the_sync_stays_inside_its_own_namespace(harness: SyncHarness) -> None:
    """One server's sync must never touch another server's slice — or anything
    else living in the instructions table."""
    first = await harness.server()
    second = await harness.server(name="other", url="https://mcp.other.example/mcp")
    await harness.sync([tool("get_weather")]).sync_server(first)
    await harness.sync([tool("translate")]).sync_server(second)

    await harness.sync([]).sync_server(first)  # the first server lost every tool

    assert await harness.mirror_titles("example") == []
    assert await harness.mirror_titles("other") == ["mcp/other/translate"]


async def test_descriptions_are_truncated_before_they_are_stored(harness: SyncHarness) -> None:
    server = await harness.server()

    await harness.sync([tool("wordy", description="x" * 5000)]).sync_server(server)

    (record,) = await harness.instructions.list_public_by_prefix(
        InstructionType.ENDPOINT, "mcp/example/"
    )
    assert len(json.loads(record.content)["description"]) == DESCRIPTION_MAX_CHARS


async def test_a_server_with_too_many_tools_is_refused_outright(harness: SyncHarness) -> None:
    """Refused, not truncated: a partial mirror would silently misrepresent
    what the server offers."""
    server = await harness.server()
    flood = [tool(f"tool_{index:03d}") for index in range(MAX_TOOLS_PER_SERVER + OVERFLOW_TOOLS)]

    with pytest.raises(McpError, match="cap is 100"):
        await harness.sync(flood).sync_server(server)

    assert await harness.mirror_titles("example") == []
    refused = await harness.store.get_by_name("example")
    assert refused is not None and refused.last_sync_error is not None
    assert "refusing" in refused.last_sync_error


async def test_a_failed_listing_lands_on_the_server_row(harness: SyncHarness) -> None:
    server = await harness.server()

    with pytest.raises(McpError):
        await harness.sync(McpError("HTTP 401: who are you")).sync_server(server)

    failed = await harness.store.get_by_name("example")
    assert failed is not None and failed.last_sync_error == "HTTP 401: who are you"


async def test_sync_all_survives_a_broken_server(harness: SyncHarness) -> None:
    await harness.server()
    healthy = await harness.server(name="other", url="https://mcp.other.example/mcp")

    class SplitClient(ScriptedMcpClient):
        async def list_tools(self, url: str, headers: dict[str, str]) -> list[McpToolDescriptor]:
            if url == URL:
                raise McpError("down")
            return [tool("translate")]

    sync = McpToolSync(harness.store, SplitClient([]), harness.instructions)
    await sync.sync_all()

    assert await harness.mirror_titles("other") == ["mcp/other/translate"]
    broken = await harness.store.get_by_name("example")
    assert broken is not None and broken.last_sync_error == "down"
    synced = await harness.store.get_by_name(healthy.name)
    assert synced is not None and synced.last_sync_error is None
