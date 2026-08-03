"""mcp_add and the external_call dispatch of mirrored MCP tool records."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.instructions.api import InstructionType
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.mcp.api import McpError, McpToolDescriptor, McpToolResult
from octoforge_core.mcp.executor import McpMirrorCallExecutor
from octoforge_core.mcp.store import SqlAlchemyMcpServerStore
from octoforge_core.mcp.sync import McpToolSync
from octoforge_core.mcp.tools import McpAddTool
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tools import ExternalCallTool
from octoforge_core.secrets.api import SecretNotFoundError
from octoforge_core.tools.base import ToolContext

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
URL = "https://mcp.example.com/mcp"
GUARD = SsrfGuard(allowed_prefixes=("https://mcp.example.com",))
CHANNEL = "web"
DIALOG_ID = "dlg-1"

WEATHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
    "required": ["city"],
    "additionalProperties": False,
}


class LenientEmbedder:
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


class RecordingMcpClient:
    """McpClient stub: scripted tools, recorded calls."""

    def __init__(
        self,
        tools: list[McpToolDescriptor] | None = None,
        result: McpToolResult | McpError | None = None,
    ) -> None:
        self.tools = tools or []
        self.result = result or McpToolResult(text="sunny", is_error=False)
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []

    async def list_tools(self, url: str, headers: dict[str, str]) -> list[McpToolDescriptor]:
        return list(self.tools)

    async def call_tool(
        self, url: str, headers: dict[str, str], tool: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        self.calls.append((url, tool, arguments, headers))
        if isinstance(self.result, McpError):
            raise self.result
        return self.result


class OneSecretStore:
    """SecretStore stub with a single (code, host) value."""

    def __init__(self, code: str, host: str, value: str) -> None:
        self._code, self._host, self._value = code, host, value

    async def resolve(self, user_id: str, code: str, host: str) -> str:
        if code == self._code and host == self._host:
            return self._value
        raise SecretNotFoundError(code)


class Installation:
    """One in-memory installation wired far enough for the mcp tools."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.mcp_store = SqlAlchemyMcpServerStore(factory)
        self.instructions = LocalInstructionService(
            SqlAlchemyInstructionStore(factory), LenientEmbedder()
        )
        self.identities = SqlAlchemyIdentityStore(factory)

    async def context(self) -> ToolContext:
        user = await self.identities.create_user()
        return ToolContext(user_id=user.id, channel=CHANNEL, dialog_id=DIALOG_ID)

    def add_tool(self, client: RecordingMcpClient) -> McpAddTool:
        sync = McpToolSync(self.mcp_store, client, self.instructions)
        return McpAddTool(self.mcp_store, sync, GUARD)

    def call_tool(
        self, client: RecordingMcpClient, secrets: "OneSecretStore | None" = None
    ) -> ExternalCallTool:
        executor = ExternalCallExecutor(
            service=self.instructions,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_no_http)),
            guard=GUARD,
            delegates={"mcp": McpMirrorCallExecutor(self.mcp_store, client, secrets)},
        )
        return ExternalCallTool(executor)


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError("the MCP path must not go through plain HTTP")


@pytest.fixture
async def installation() -> AsyncIterator[Installation]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield Installation(create_session_factory(engine))
    await engine.dispose()


def weather_tool() -> McpToolDescriptor:
    return McpToolDescriptor(
        name="get_weather", description="Weather for a city", input_schema=WEATHER_SCHEMA
    )


async def test_mcp_add_registers_and_mirrors(installation: Installation) -> None:
    tool = installation.add_tool(RecordingMcpClient(tools=[weather_tool()]))

    report = await tool.execute({"url": URL}, await installation.context())

    assert "1 tools mirrored" in report
    assert "mcp/mcp-example-com/get_weather" in report
    records = await installation.instructions.list_public_by_prefix(
        InstructionType.ENDPOINT, "mcp/mcp-example-com/"
    )
    assert len(records) == 1


async def test_mcp_add_dedups_across_users(installation: Installation) -> None:
    tool = installation.add_tool(RecordingMcpClient(tools=[weather_tool()]))

    await tool.execute({"url": URL, "name": "example"}, await installation.context())
    await tool.execute({"url": URL, "name": "renamed"}, await installation.context())

    servers = await installation.mcp_store.list_all()
    assert len(servers) == 1
    assert servers[0].name == "example"  # the first name won; the URL is the identity


async def test_mcp_add_reports_a_failed_listing_and_keeps_the_server(
    installation: Installation,
) -> None:
    class DownClient(RecordingMcpClient):
        async def list_tools(self, url: str, headers: dict[str, str]) -> list[McpToolDescriptor]:
            raise McpError("HTTP 401: who are you")

    report = await installation.add_tool(DownClient()).execute(
        {"url": URL, "auth": {"secret": "example_token"}}, await installation.context()
    )

    assert "listing its tools failed" in report
    assert "HTTP 401" in report
    assert "example_token" in report  # the /secrets hint names the code
    assert len(await installation.mcp_store.list_all()) == 1


async def test_mcp_add_refuses_a_blocked_url(installation: Installation) -> None:
    tool = installation.add_tool(RecordingMcpClient())

    report = await tool.execute({"url": "http://127.0.0.1/mcp"}, await installation.context())

    assert report.startswith("error:")
    assert await installation.mcp_store.list_all() == []


async def test_external_call_executes_a_mirrored_tool(installation: Installation) -> None:
    context = await installation.context()
    client = RecordingMcpClient(tools=[weather_tool()])
    await installation.add_tool(client).execute({"url": URL, "name": "example"}, context)

    output = await installation.call_tool(client).execute(
        {"name": "mcp/example/get_weather", "params": {"city": "Lisbon", "days": 3}}, context
    )

    assert output == "sunny"  # no fake HTTP status in front of a non-HTTP transport
    ((url, tool, arguments, _),) = ((call[0], call[1], call[2], call[3]) for call in client.calls)
    assert (url, tool) == (URL, "get_weather")
    assert arguments == {"city": "Lisbon", "days": 3}  # structured values pass through


async def test_a_bad_call_gets_the_contract_back(installation: Installation) -> None:
    context = await installation.context()
    client = RecordingMcpClient(tools=[weather_tool()])
    await installation.add_tool(client).execute({"url": URL, "name": "example"}, context)

    with pytest.raises(ExternalCallError) as excinfo:
        await installation.call_tool(client).execute(
            {"name": "mcp/example/get_weather", "params": {"town": "Lisbon"}}, context
        )

    message = str(excinfo.value)
    assert "missing required arguments: city" in message
    assert "unknown arguments: town" not in message  # the first failure answers
    assert "input_schema" in message  # the full contract rides along
    assert client.calls == []


async def test_a_server_side_tool_error_is_data_with_the_contract(
    installation: Installation,
) -> None:
    context = await installation.context()
    client = RecordingMcpClient(
        tools=[weather_tool()], result=McpToolResult(text="no such city", is_error=True)
    )
    await installation.add_tool(client).execute({"url": URL, "name": "example"}, context)

    output = await installation.call_tool(client).execute(
        {"name": "mcp/example/get_weather", "params": {"city": "Nowhere"}}, context
    )

    assert "mcp tool error: no such city" in output
    assert "input_schema" in output


async def test_a_transport_failure_hints_at_a_stale_mirror(installation: Installation) -> None:
    context = await installation.context()
    client = RecordingMcpClient(tools=[weather_tool()], result=McpError("Unknown tool"))
    await installation.add_tool(client).execute({"url": URL, "name": "example"}, context)

    with pytest.raises(ExternalCallError, match="stale"):
        await installation.call_tool(client).execute(
            {"name": "mcp/example/get_weather", "params": {"city": "Lisbon"}}, context
        )


async def test_an_authenticated_server_uses_the_callers_own_token(
    installation: Installation,
) -> None:
    context = await installation.context()
    client = RecordingMcpClient(tools=[weather_tool()])
    await installation.add_tool(client).execute(
        {"url": URL, "name": "example", "auth": {"secret": "example_token"}}, context
    )
    secrets = OneSecretStore("example_token", "mcp.example.com", "token-value")

    await installation.call_tool(client, secrets=secrets).execute(
        {"name": "mcp/example/get_weather", "params": {"city": "Lisbon"}}, context
    )

    (_, _, _, headers) = client.calls[0]
    assert headers == {"Authorization": "Bearer token-value"}


async def test_a_missing_token_answers_with_the_secrets_guidance(
    installation: Installation,
) -> None:
    context = await installation.context()
    client = RecordingMcpClient(tools=[weather_tool()])
    await installation.add_tool(client).execute(
        {"url": URL, "name": "example", "auth": {"secret": "example_token"}}, context
    )
    secrets = OneSecretStore("other_code", "mcp.example.com", "value")

    with pytest.raises(ExternalCallError, match="/secrets"):
        await installation.call_tool(client, secrets=secrets).execute(
            {"name": "mcp/example/get_weather", "params": {"city": "Lisbon"}}, context
        )


async def test_classic_endpoints_still_take_strings_only(installation: Installation) -> None:
    context = await installation.context()
    await installation.instructions.save_public(
        InstructionType.ENDPOINT,
        "wttr_in_weather",
        '{"method": "GET", "url_template": "https://mcp.example.com/{city}", '
        '"params_schema": {"city": {"type": "string", "required": true}}}',
    )

    with pytest.raises(ExternalCallError, match="must be strings"):
        await installation.call_tool(RecordingMcpClient()).execute(
            {"name": "wttr_in_weather", "params": {"city": ["Lisbon"]}}, context
        )


async def test_an_unknown_kind_is_refused(installation: Installation) -> None:
    context = await installation.context()
    await installation.instructions.save_public(
        InstructionType.ENDPOINT, "strange", '{"kind": "grpc", "service": "x"}'
    )

    with pytest.raises(ExternalCallError, match="no executor"):
        await installation.call_tool(RecordingMcpClient()).execute(
            {"name": "strange", "params": {}}, context
        )


async def test_a_gone_server_is_reported_not_crashed(installation: Installation) -> None:
    context = await installation.context()
    await installation.instructions.save_public(
        InstructionType.ENDPOINT,
        "mcp/ghost/tool",
        '{"kind": "mcp", "server": "ghost", "tool": "tool", "input_schema": {}}',
    )

    with pytest.raises(ExternalCallError, match="not registered"):
        await installation.call_tool(RecordingMcpClient()).execute(
            {"name": "mcp/ghost/tool", "params": {}}, context
        )
