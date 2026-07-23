"""Tests for the runtime tool adapters over the instructions facade."""

import json
from datetime import UTC, datetime
from http import HTTPStatus

import httpx
import pytest

from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionType,
    SearchHit,
)
from octoforge_core.instructions.tools import (
    DELETE_NAME,
    MAX_K,
    MAX_OUTPUT_CHARS,
    NO_HITS_MESSAGE,
    NOT_FOUND_MESSAGE,
    SAVE_NAME,
    SEARCH_NAME,
    InstructionDeleteTool,
    InstructionSaveTool,
    InstructionSearchTool,
)
from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tools import CALL_NAME as EXTERNAL_CALL_NAME
from octoforge_core.net.tools import ExternalCallTool
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

CTX = ToolContext(user_id="user-test", channel="web", dialog_id="dlg-test")
DEFAULT_K = 5
CUSTOM_K = 3
SAVED_VERSION = 3
PUBLIC_IP = "93.184.216.34"
TOOL_NAME = "wttr_in_weather"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

HIT = SearchHit(
    instruction=Instruction(
        id="id-1",
        type=InstructionType.ENDPOINT,
        title=TOOL_NAME,
        content='{"method": "GET", "url_template": "https://wttr.in/{city}?format=j2"}',
        tags=("http", "weather"),
        version=1,
        usage_count=0,
        success_count=0,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    ),
    score=0.875,
)


class FakeInstructionService:
    """InstructionService stub with scripted hits and recorded calls."""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.search_calls: list[tuple[str, str, int, InstructionType | None]] = []
        self.saved: list[tuple[str, InstructionType, str, str, tuple[str, ...]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.save_result: Instruction | None = None
        self.delete_missing = False

    async def search(
        self,
        user_id: str,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        self.search_calls.append((user_id, query, k, kind))
        return self.hits

    async def search_all(
        self,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError

    async def save(
        self,
        user_id: str,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        self.saved.append((user_id, kind, title, content, tags))
        assert self.save_result is not None
        return self.save_result

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        raise NotImplementedError

    async def delete(self, user_id: str, instruction_id: str) -> None:
        self.deleted.append((user_id, instruction_id))
        if self.delete_missing:
            raise InstructionNotFoundError(instruction_id)

    async def publish(self, instruction_id: str) -> Instruction:
        raise NotImplementedError


def saved_instruction() -> Instruction:
    return Instruction(
        id="id-2",
        type=InstructionType.SKILL,
        title="new scenario",
        content="do X then Y",
        tags=("x",),
        version=SAVED_VERSION,
        usage_count=0,
        success_count=0,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


class StubResolver:
    """HostResolver returning a scripted set of addresses."""

    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips

    async def resolve(self, host: str) -> tuple[str, ...]:
        return self._ips


def make_external_call_tool(body: str = "{}") -> ExternalCallTool:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(HTTPStatus.OK, text=body))
    )
    records = {
        TOOL_NAME: json.dumps(
            {
                "method": "GET",
                "url_template": "https://wttr.in/{city}?format=j2",
                "params_schema": {"city": {"type": "string", "required": True}},
                "auth": "none",
            }
        )
    }
    executor = ExternalCallExecutor(
        service=_ToolServingService(records),
        http_client=http_client,
        guard=SsrfGuard(resolver=StubResolver((PUBLIC_IP,))),
    )
    return ExternalCallTool(executor=executor)


class _ToolServingService(FakeInstructionService):
    def __init__(self, records: dict[str, str]) -> None:
        super().__init__()
        self._records = records

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        if name not in self._records:
            raise InstructionNotFoundError(name)
        return Instruction(
            id=f"id-{name}",
            type=InstructionType.ENDPOINT,
            title=name,
            content=self._records[name],
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        )


# --- instruction_search ---------------------------------------------------


def test_search_skill_spec() -> None:
    tool = InstructionSearchTool(service=FakeInstructionService(), default_k=DEFAULT_K)

    assert tool.spec.name == SEARCH_NAME
    assert tool.spec.parameters_schema["required"] == ["query"]


async def test_search_skill_formats_hits() -> None:
    service = FakeInstructionService(hits=[HIT])
    tool = InstructionSearchTool(service=service, default_k=DEFAULT_K)

    output = await tool.execute({"query": "weather"}, CTX)

    assert service.search_calls == [("user-test", "weather", DEFAULT_K, None)]
    assert "[endpoint]" in output
    assert TOOL_NAME in output
    assert "id: id-1" in output  # ids are shown: instruction_delete targets them
    assert "http, weather" in output
    assert "score" not in output  # scores are omitted: rerank logits are uninformative
    assert "url_template" in output  # full content


async def test_search_passes_the_type_filter() -> None:
    service = FakeInstructionService(hits=[HIT])
    tool = InstructionSearchTool(service=service, default_k=DEFAULT_K)

    await tool.execute({"query": "weather", "type": "endpoint"}, CTX)

    assert service.search_calls == [("user-test", "weather", DEFAULT_K, InstructionType.ENDPOINT)]


async def test_search_skill_returns_full_content_without_truncation() -> None:
    long_content = "line one\n" + ("x" * 400) + "\nline tail"
    hit = SearchHit(
        instruction=Instruction(
            id="id-long",
            type=InstructionType.SKILL,
            title="long scenario",
            content=long_content,
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        ),
        score=0.5,
    )
    tool = InstructionSearchTool(service=FakeInstructionService(hits=[hit]), default_k=DEFAULT_K)

    output = await tool.execute({"query": "scenario"}, CTX)

    assert "x" * 400 in output  # beyond the old 300-char snippet cap
    assert "line one" in output
    assert "line tail" in output


async def test_search_skill_explicit_k_overrides_default() -> None:
    service = FakeInstructionService()
    tool = InstructionSearchTool(service=service, default_k=DEFAULT_K)

    await tool.execute({"query": "q", "k": CUSTOM_K}, CTX)
    await tool.execute({"query": "q", "k": MAX_K}, CTX)

    assert service.search_calls == [
        ("user-test", "q", CUSTOM_K, None),
        ("user-test", "q", MAX_K, None),
    ]


async def test_search_skill_no_hits_message() -> None:
    tool = InstructionSearchTool(service=FakeInstructionService(), default_k=DEFAULT_K)

    output = await tool.execute({"query": "nothing"}, CTX)

    assert output == NO_HITS_MESSAGE
    assert output == "no matching instructions or datasets"  # covers knowledge/endpoints too


def make_hit(title: str, content: str = "content") -> SearchHit:
    """Build a minimal skill hit with the given title and content."""
    return SearchHit(
        instruction=Instruction(
            id=f"id-{title}",
            type=InstructionType.SKILL,
            title=title,
            content=content,
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        ),
        score=0.5,
    )


async def test_search_caps_the_merged_list_at_k() -> None:
    # the stub ignores k and hands back more hits than requested: the merged
    # list (instructions + datasets) must still be capped at k entries
    hits = [make_hit(f"scenario {index}") for index in range(CUSTOM_K + 1)]
    tool = InstructionSearchTool(service=FakeInstructionService(hits=hits), default_k=DEFAULT_K)

    output = await tool.execute({"query": "q", "k": CUSTOM_K}, CTX)

    assert "scenario 0" in output
    assert f"scenario {CUSTOM_K - 1}" in output
    assert f"scenario {CUSTOM_K}" not in output


async def test_search_truncates_long_output_with_a_note() -> None:
    big_content = "x" * MAX_OUTPUT_CHARS
    hits = [make_hit("first", big_content), make_hit("second", big_content)]
    tool = InstructionSearchTool(service=FakeInstructionService(hits=hits), default_k=DEFAULT_K)

    output = await tool.execute({"query": "q", "k": 2}, CTX)

    assert "first" in output  # the first hit is always emitted whole
    assert "second" not in output
    assert "truncated" in output
    assert "1 more hit(s)" in output


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 42},
        {"query": "q", "k": True},
        {"query": "q", "k": "3"},
        {"query": "q", "k": 0},
        {"query": "q", "k": MAX_K + 1},
        {"query": "q", "type": "dataset"},
    ],
)
async def test_search_skill_invalid_arguments_rejected(arguments: dict[str, object]) -> None:
    tool = InstructionSearchTool(service=FakeInstructionService(), default_k=DEFAULT_K)

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)


# --- instruction_save ------------------------------------------------------


def test_save_skill_spec() -> None:
    tool = InstructionSaveTool(service=FakeInstructionService())

    assert tool.spec.name == SAVE_NAME
    assert tool.spec.parameters_schema["required"] == ["type", "title", "content"]


async def test_save_skill_saves_and_confirms_version() -> None:
    service = FakeInstructionService()
    service.save_result = saved_instruction()
    tool = InstructionSaveTool(service=service)

    output = await tool.execute(
        {
            "type": "skill",
            "title": "new scenario",
            "content": "do X then Y",
            "tags": ["x"],
        },
        CTX,
    )

    assert service.saved == [
        ("user-test", InstructionType.SKILL, "new scenario", "do X then Y", ("x",))
    ]
    assert "skill" in output
    assert "new scenario" in output
    assert f"version {SAVED_VERSION}" in output


async def test_save_skill_defaults_tags_to_empty() -> None:
    service = FakeInstructionService()
    service.save_result = saved_instruction()
    tool = InstructionSaveTool(service=service)

    await tool.execute({"type": "knowledge", "title": "t", "content": "c"}, CTX)

    assert service.saved[0][4] == ()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"type": "dataset", "title": "t", "content": "c"},
        {"type": "skill", "title": "", "content": "c"},
        {"type": "skill", "title": "t", "content": ""},
        {"type": "skill", "title": "t", "content": "   "},
        {"type": "skill", "title": "t", "content": "c", "tags": "not-a-list"},
        {"type": "skill", "title": "t", "content": "c", "tags": ["ok", 1]},
    ],
)
async def test_save_skill_invalid_arguments_rejected(arguments: dict[str, object]) -> None:
    tool = InstructionSaveTool(service=FakeInstructionService())

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)


# --- instruction_delete ----------------------------------------------------


def test_delete_spec() -> None:
    tool = InstructionDeleteTool(service=FakeInstructionService())

    assert tool.spec.name == DELETE_NAME
    assert tool.spec.parameters_schema["required"] == ["id"]


async def test_delete_removes_the_own_record() -> None:
    service = FakeInstructionService()
    tool = InstructionDeleteTool(service=service)

    output = await tool.execute({"id": "id-1"}, CTX)

    assert service.deleted == [("user-test", "id-1")]
    assert output == "instruction deleted"


async def test_delete_reports_a_missing_or_foreign_record() -> None:
    service = FakeInstructionService()
    service.delete_missing = True
    tool = InstructionDeleteTool(service=service)

    output = await tool.execute({"id": "id-foreign"}, CTX)

    assert output == NOT_FOUND_MESSAGE


@pytest.mark.parametrize("arguments", [{}, {"id": ""}, {"id": "   "}, {"id": 42}])
async def test_delete_invalid_arguments_rejected(arguments: dict[str, object]) -> None:
    tool = InstructionDeleteTool(service=FakeInstructionService())

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)


# --- external_call ---------------------------------------------------------


def test_external_call_skill_spec() -> None:
    tool = make_external_call_tool()

    assert tool.spec.name == EXTERNAL_CALL_NAME
    assert tool.spec.parameters_schema["required"] == ["name"]


async def test_external_call_skill_returns_status_and_body() -> None:
    tool = make_external_call_tool(body='{"temp_C": "11"}')

    output = await tool.execute({"name": TOOL_NAME, "params": {"city": "London"}}, CTX)

    assert output == f"HTTP {HTTPStatus.OK}\n" + '{"temp_C": "11"}'


async def test_external_call_tool_propagates_unknown_tool() -> None:
    tool = make_external_call_tool()

    with pytest.raises(InstructionNotFoundError):
        await tool.execute({"name": "no_such_tool"}, CTX)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"name": ""},
        {"name": 42},
        {"name": TOOL_NAME, "params": {"city": 1}},
        {"name": TOOL_NAME, "params": ["city"]},
    ],
)
async def test_external_call_skill_invalid_arguments_rejected(
    arguments: dict[str, object],
) -> None:
    tool = make_external_call_tool()

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)
