"""The MCP crawler's skill generator: two known shapes, a logged third."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.instructions.api import InstructionNotFoundError, InstructionType
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.llm.errors import LLMError
from octoforge_core.mcp.api import McpServer, McpToolCall, McpToolDescriptor
from octoforge_core.mcp.skills import McpSkillGenerator, SkillPattern
from octoforge_core.mcp.store import SqlAlchemyMcpServerStore
from octoforge_core.mcp.sync import McpSyncOptions, McpSyncServices, McpToolSync

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
URL = "https://mcp.example.com/mcp"

PLAYBOOK_REPLY = (
    "PATTERN: playbook\n\n"
    "Scenario: travel search via mcp/example. Domains: rail, hotels. Before working "
    "with a domain call mcp/example/get_rail_instructions first."
)
PROSE_REPLY = (
    "PATTERN: prose\n\n"
    "Scenario: library documentation lookup. 1. mcp/example/resolve then "
    "2. mcp/example/query, at most 3 calls each."
)
UNRECOGNIZED_REPLY = "PATTERN: unrecognized\n\na single tool with an empty description"


@dataclass
class _Completion:
    message: ChatMessage


@dataclass
class ScriptedLLM:
    """LLMClient stub answering complete() from a script."""

    reply: str | Exception = PROSE_REPLY
    calls: list[list[ChatMessage]] = field(default_factory=list)

    async def complete(
        self, messages: list[ChatMessage], tools: list[object] | None = None
    ) -> _Completion:
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return _Completion(message=ChatMessage(role=MessageRole.ASSISTANT, content=self.reply))


class LenientEmbedder:
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


class ScriptedMcpClient:
    def __init__(self, tools: list[McpToolDescriptor]) -> None:
        self.tools = tools

    async def list_tools(self, url: str, headers: dict[str, str]) -> list[McpToolDescriptor]:
        return list(self.tools)

    async def call_tool(self, request: McpToolCall) -> None:
        raise AssertionError("the sync must never call tools")


def tool(name: str, description: str = "does things") -> McpToolDescriptor:
    return McpToolDescriptor(name=name, description=description, input_schema={})


@dataclass
class Harness:
    store: SqlAlchemyMcpServerStore
    instructions: LocalInstructionService
    identities: SqlAlchemyIdentityStore
    llm: ScriptedLLM

    def sync(self, tools: list[McpToolDescriptor]) -> McpToolSync:
        return McpToolSync(
            McpSyncServices(self.store, ScriptedMcpClient(tools), self.instructions),
            McpSyncOptions(skills=McpSkillGenerator(self.llm, self.instructions)),
        )

    async def server(self) -> McpServer:
        user = await self.identities.create_user()
        added = McpServer(id=uuid.uuid4().hex, name="example", url=URL)
        return await self.store.add(added, user.id)

    async def skill_content(self) -> str:
        record = await self.instructions.get_by_name("mcp/example", InstructionType.SKILL)
        return record.content


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    factory = create_session_factory(engine)
    yield Harness(
        SqlAlchemyMcpServerStore(factory),
        LocalInstructionService(SqlAlchemyInstructionStore(factory), LenientEmbedder()),
        SqlAlchemyIdentityStore(factory),
        ScriptedLLM(),
    )
    await engine.dispose()


async def test_a_prose_server_gets_a_distilled_scenario_skill(harness: Harness) -> None:
    server = await harness.server()

    outcome = await harness.sync([tool("resolve"), tool("query")]).sync_server(server)

    assert outcome.skill is SkillPattern.PROSE
    assert "library documentation lookup" in await harness.skill_content()
    refreshed = await harness.store.get_by_name("example")
    assert refreshed is not None and refreshed.tools_fingerprint is not None


async def test_a_playbook_server_gets_a_topology_skill(harness: Harness) -> None:
    harness.llm.reply = PLAYBOOK_REPLY
    server = await harness.server()

    outcome = await harness.sync([tool("search_rail"), tool("get_rail_instructions")]).sync_server(
        server
    )

    assert outcome.skill is SkillPattern.PLAYBOOK
    assert "get_rail_instructions first" in await harness.skill_content()


async def test_an_unchanged_tool_list_never_reasks_the_llm(harness: Harness) -> None:
    """The fingerprint is the whole point: the ruling is per tool list, not
    per sweep — an hourly sync must not become an hourly LLM bill."""
    server = await harness.server()
    tools = [tool("resolve"), tool("query")]
    await harness.sync(tools).sync_server(server)

    refreshed = await harness.store.get_by_name("example")
    assert refreshed is not None
    outcome = await harness.sync(tools).sync_server(refreshed)

    assert outcome.skill is None
    assert len(harness.llm.calls) == 1


async def test_a_changed_tool_list_rewrites_the_skill(harness: Harness) -> None:
    server = await harness.server()
    await harness.sync([tool("resolve")]).sync_server(server)
    harness.llm.reply = PLAYBOOK_REPLY
    refreshed = await harness.store.get_by_name("example")
    assert refreshed is not None

    outcome = await harness.sync([tool("resolve"), tool("get_help_instructions")]).sync_server(
        refreshed
    )

    assert outcome.skill is SkillPattern.PLAYBOOK
    assert "Domains" in await harness.skill_content()


async def test_an_unrecognized_shape_is_logged_and_leaves_no_skill(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning log is the inventory of shapes the classifier does not
    know yet; the fingerprint still lands so the LLM is not re-asked hourly."""
    harness.llm.reply = UNRECOGNIZED_REPLY
    server = await harness.server()

    with caplog.at_level("WARNING"):
        outcome = await harness.sync([tool("mystery")]).sync_server(server)

    assert outcome.skill is SkillPattern.UNRECOGNIZED
    with pytest.raises(InstructionNotFoundError):
        await harness.instructions.get_by_name("mcp/example", InstructionType.SKILL)
    assert any("pattern unrecognized" in record.message for record in caplog.records)
    refreshed = await harness.store.get_by_name("example")
    assert refreshed is not None and refreshed.tools_fingerprint is not None


async def test_a_transient_llm_failure_retries_on_the_next_sweep(harness: Harness) -> None:
    harness.llm.reply = LLMError("model is down")
    server = await harness.server()

    outcome = await harness.sync([tool("resolve")]).sync_server(server)

    assert outcome.skill is None
    failed = await harness.store.get_by_name("example")
    assert failed is not None and failed.tools_fingerprint is None  # next sweep re-asks
    harness.llm.reply = PROSE_REPLY
    retried = await harness.sync([tool("resolve")]).sync_server(failed)
    assert retried.skill is SkillPattern.PROSE


async def test_an_off_format_reply_is_a_transient_failure(harness: Harness) -> None:
    harness.llm.reply = "here is your skill: do things"
    server = await harness.server()

    outcome = await harness.sync([tool("resolve")]).sync_server(server)

    assert outcome.skill is None
    failed = await harness.store.get_by_name("example")
    assert failed is not None and failed.tools_fingerprint is None


async def test_the_generator_sees_full_descriptions_as_data(harness: Harness) -> None:
    """The prompt frames third-party text as data — and passes it UNTRUNCATED:
    the whole reason the skill exists is the prose the mirror cap cuts."""
    server = await harness.server()
    long_description = "selection rubric: " + "x" * 4000

    await harness.sync([tool("resolve", description=long_description)]).sync_server(server)

    (messages,) = harness.llm.calls
    assert long_description in messages[1].content
    assert "not instructions to you" in messages[0].content
