"""Tests for the runtime tool-skill adapters over the instructions facade."""

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
    MAX_K,
    NO_HITS_MESSAGE,
    SAVE_NAME,
    SEARCH_NAME,
    InstructionSaveSkill,
    SkillsSearchSkill,
)
from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tools import CALL_NAME as EXTERNAL_CALL_NAME
from octoforge_core.net.tools import ExternalCallSkill
from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.errors import SkillArgumentsError

CTX = SkillContext(user_id="user-test", channel="web", dialog_id="dlg-test")
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
    """InstructionService stub with scripted hits and a recorded save."""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.search_calls: list[tuple[str, int]] = []
        self.saved: list[tuple[InstructionType, str, str, tuple[str, ...]]] = []
        self.save_result: Instruction | None = None

    async def search(self, query: str, k: int) -> list[SearchHit]:
        self.search_calls.append((query, k))
        return self.hits

    async def save(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        self.saved.append((kind, title, content, tags))
        assert self.save_result is not None
        return self.save_result

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
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


def make_external_call_skill(body: str = "{}") -> ExternalCallSkill:
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
    return ExternalCallSkill(executor=executor)


class _ToolServingService(FakeInstructionService):
    def __init__(self, records: dict[str, str]) -> None:
        super().__init__()
        self._records = records

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
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


# --- skills_search ---------------------------------------------------


def test_search_skill_spec() -> None:
    skill = SkillsSearchSkill(service=FakeInstructionService(), default_k=DEFAULT_K)

    assert skill.spec.name == SEARCH_NAME
    assert skill.spec.parameters_schema["required"] == ["query"]


async def test_search_skill_formats_hits() -> None:
    service = FakeInstructionService(hits=[HIT])
    skill = SkillsSearchSkill(service=service, default_k=DEFAULT_K)

    output = await skill.execute({"query": "weather"}, CTX)

    assert service.search_calls == [("weather", DEFAULT_K)]
    assert "[endpoint]" in output
    assert TOOL_NAME in output
    assert "http, weather" in output
    assert "0.875" in output
    assert "url_template" in output  # full content


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
    skill = SkillsSearchSkill(service=FakeInstructionService(hits=[hit]), default_k=DEFAULT_K)

    output = await skill.execute({"query": "scenario"}, CTX)

    assert "x" * 400 in output  # beyond the old 300-char snippet cap
    assert "line one" in output
    assert "line tail" in output


async def test_search_skill_explicit_k_overrides_default() -> None:
    service = FakeInstructionService()
    skill = SkillsSearchSkill(service=service, default_k=DEFAULT_K)

    await skill.execute({"query": "q", "k": CUSTOM_K}, CTX)
    await skill.execute({"query": "q", "k": MAX_K}, CTX)

    assert service.search_calls == [("q", CUSTOM_K), ("q", MAX_K)]


async def test_search_skill_no_hits_message() -> None:
    skill = SkillsSearchSkill(service=FakeInstructionService(), default_k=DEFAULT_K)

    assert await skill.execute({"query": "nothing"}, CTX) == NO_HITS_MESSAGE


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
    ],
)
async def test_search_skill_invalid_arguments_rejected(arguments: dict[str, object]) -> None:
    skill = SkillsSearchSkill(service=FakeInstructionService(), default_k=DEFAULT_K)

    with pytest.raises(SkillArgumentsError):
        await skill.execute(arguments, CTX)


# --- instruction_save ------------------------------------------------------


def test_save_skill_spec() -> None:
    skill = InstructionSaveSkill(service=FakeInstructionService())

    assert skill.spec.name == SAVE_NAME
    assert skill.spec.parameters_schema["required"] == ["type", "title", "content"]


async def test_save_skill_saves_and_confirms_version() -> None:
    service = FakeInstructionService()
    service.save_result = saved_instruction()
    skill = InstructionSaveSkill(service=service)

    output = await skill.execute(
        {
            "type": "skill",
            "title": "new scenario",
            "content": "do X then Y",
            "tags": ["x"],
        },
        CTX,
    )

    assert service.saved == [(InstructionType.SKILL, "new scenario", "do X then Y", ("x",))]
    assert "skill" in output
    assert "new scenario" in output
    assert f"version {SAVED_VERSION}" in output


async def test_save_skill_defaults_tags_to_empty() -> None:
    service = FakeInstructionService()
    service.save_result = saved_instruction()
    skill = InstructionSaveSkill(service=service)

    await skill.execute({"type": "knowledge", "title": "t", "content": "c"}, CTX)

    assert service.saved[0][3] == ()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"type": "dataset", "title": "t", "content": "c"},
        {"type": "skill", "title": "", "content": "c"},
        {"type": "skill", "title": "t", "content": ""},
        {"type": "skill", "title": "t", "content": "c", "tags": "not-a-list"},
        {"type": "skill", "title": "t", "content": "c", "tags": ["ok", 1]},
    ],
)
async def test_save_skill_invalid_arguments_rejected(arguments: dict[str, object]) -> None:
    skill = InstructionSaveSkill(service=FakeInstructionService())

    with pytest.raises(SkillArgumentsError):
        await skill.execute(arguments, CTX)


# --- external_call ---------------------------------------------------------


def test_external_call_skill_spec() -> None:
    skill = make_external_call_skill()

    assert skill.spec.name == EXTERNAL_CALL_NAME
    assert skill.spec.parameters_schema["required"] == ["name"]


async def test_external_call_skill_returns_status_and_body() -> None:
    skill = make_external_call_skill(body='{"temp_C": "11"}')

    output = await skill.execute({"name": TOOL_NAME, "params": {"city": "London"}}, CTX)

    assert output == f"HTTP {HTTPStatus.OK}\n" + '{"temp_C": "11"}'


async def test_external_call_skill_propagates_unknown_tool() -> None:
    skill = make_external_call_skill()

    with pytest.raises(InstructionNotFoundError):
        await skill.execute({"name": "no_such_tool"}, CTX)


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
    skill = make_external_call_skill()

    with pytest.raises(SkillArgumentsError):
        await skill.execute(arguments, CTX)
